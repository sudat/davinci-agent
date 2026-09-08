"""UX phase 2.5 slice-1: consultation journal store (failure-first).

The consultation journal is append-only runtime state under
``episodes/<id>/consultation/`` — reload-safe (the full journal
reconstructs state), never an authoritative artifact family. Budget
counters are cumulative for the episode's consultation phase and are
NEVER reset by a retry/regeneration.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from services.episode_cockpit.consultation_store import (
    DEFAULT_INTERVALS_LIMIT,
    DEFAULT_LLM_CALLS_LIMIT,
    DEFAULT_WALL_SECONDS_LIMIT,
    ConsultationBudgetEventV1,
    ConsultationBudgetLimits,
    ConsultationJudgmentV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    ConsultationScope,
    append_budget_event,
    append_consultation,
    append_judgment,
    append_proposal_set,
    budget_used,
    consultation_view,
    consume_budget,
    ensure_budget_available,
    load_budget_limits,
    load_consultations,
    load_judgments,
    require_consultation,
    require_proposal,
)
from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
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


@pytest.fixture
def episode_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "episodes" / "ep-consul-01"
    directory.mkdir(parents=True)
    return directory


def _proposal(n: int) -> ConsultationProposalV1:
    return ConsultationProposalV1(
        proposal_id=f"prop-{n}",
        title=f"案{n}",
        summary=f"要旨{n}",
        details=ConsultationProposalDetails(**_DETAILS),
    )


def _record(consultation_id: str = "c-1") -> ConsultationRecordV1:
    return ConsultationRecordV1(
        consultation_id=consultation_id, created_at="2026-09-08T00:00:00+00:00",
        message="ブログ告知用に短くしたい",
    )


def _seed_consultation(episode_dir: Path, consultation_id: str = "c-1") -> None:
    append_consultation(episode_dir, _record(consultation_id))
    append_proposal_set(
        episode_dir,
        ConsultationProposalSetV1(
            consultation_id=consultation_id,
            created_at="2026-09-08T00:00:01+00:00",
            proposals=(_proposal(1), _proposal(2)),
        ),
    )


# ---------------------------------------------------------------------------
# append-only journal: reload restores the full state
# ---------------------------------------------------------------------------


def test_reload_restores_full_journal(episode_dir: Path) -> None:
    _seed_consultation(episode_dir)
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1(
            judgment_id="j-1", consultation_id="c-1", proposal_id="prop-1",
            decision="adopt",
            scope=ConsultationScope(composition=True, appearance=False, audio=False),
            note="構成だけ採用", created_at="2026-09-08T00:00:02+00:00",
        ),
    )
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1(
            judgment_id="j-2", consultation_id="c-1", proposal_id=None,
            decision="both_wrong", scope=ConsultationScope(), note=None,
            created_at="2026-09-08T00:00:03+00:00",
        ),
    )

    consultations = load_consultations(episode_dir)
    judgments = load_judgments(episode_dir)

    assert [record.consultation_id for record in consultations] == ["c-1"]
    assert consultations[0].message == "ブログ告知用に短くしたい"
    assert [j.judgment_id for j in judgments] == ["j-1", "j-2"]


def test_absent_journal_reads_as_empty(episode_dir: Path) -> None:
    assert load_consultations(episode_dir) == []
    assert load_judgments(episode_dir) == []
    assert budget_used(episode_dir) == (0, 0, 0.0)


def test_corrupt_journal_is_a_typed_error(
    episode_dir: Path, tmp_path: Path
) -> None:
    _seed_consultation(episode_dir)
    journal = episode_dir / "consultation" / "consultations.jsonl"
    journal.write_bytes(journal.read_bytes() + b"not-json\n")

    with pytest.raises(CockpitUnprocessableError) as excinfo:
        load_consultations(episode_dir)
    assert excinfo.value.code == "consultation-log-corrupt"


def test_require_consultation_unknown_is_404(episode_dir: Path) -> None:
    with pytest.raises(CockpitNotFoundError) as excinfo:
        require_consultation(episode_dir, "missing")
    assert excinfo.value.code == "consultation-not-found"


def test_require_proposal_unknown_is_typed_422(episode_dir: Path) -> None:
    _seed_consultation(episode_dir)
    with pytest.raises(CockpitUnprocessableError) as excinfo:
        require_proposal(episode_dir, "c-1", "prop-9")
    assert excinfo.value.code == "consultation-proposal-not-found"


# ---------------------------------------------------------------------------
# judgment records
# ---------------------------------------------------------------------------


def test_empty_decision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j-1", "consultation_id": "c-1",
                "proposal_id": None,
                "decision": "",
                "scope": {"composition": False, "appearance": False, "audio": False},
                "note": None, "created_at": "2026-09-08T00:00:02+00:00",
            }
        )


def test_unknown_decision_word_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j-1", "consultation_id": "c-1",
                "proposal_id": None,
                "decision": "maybe",
                "scope": {"composition": False, "appearance": False, "audio": False},
                "note": None, "created_at": "2026-09-08T00:00:02+00:00",
            }
        )


def test_scope_is_recorded_verbatim(episode_dir: Path) -> None:
    _seed_consultation(episode_dir)
    judgment = ConsultationJudgmentV1(
        judgment_id="j-1", consultation_id="c-1", proposal_id="prop-1",
        decision="revise",
        scope=ConsultationScope(composition=True, appearance=True, audio=False),
        note="見た目は未確認", created_at="2026-09-08T00:00:02+00:00",
    )
    append_judgment(episode_dir, judgment)

    reloaded = load_judgments(episode_dir)[0]

    assert reloaded == judgment
    assert reloaded.scope == ConsultationScope(
        composition=True, appearance=True, audio=False
    )


# ---------------------------------------------------------------------------
# proposals: at most two per consultation
# ---------------------------------------------------------------------------


def test_proposal_set_rejects_more_than_two() -> None:
    with pytest.raises(ValidationError):
        ConsultationProposalSetV1(
            consultation_id="c-1", created_at="2026-09-08T00:00:01+00:00",
            proposals=(_proposal(1), _proposal(2), _proposal(3)),
        )


def test_proposal_set_rejects_zero_proposals() -> None:
    with pytest.raises(ValidationError):
        ConsultationProposalSetV1(
            consultation_id="c-1", created_at="2026-09-08T00:00:01+00:00",
            proposals=(),
        )


# ---------------------------------------------------------------------------
# budget: cumulative, never reset on retry/regeneration
# ---------------------------------------------------------------------------


def test_budget_accumulates_across_regenerations(episode_dir: Path) -> None:
    consume_budget(
        episode_dir, "c-1", llm_calls=1, intervals=1, wall_seconds=0.5
    )
    consume_budget(
        episode_dir, "c-2", llm_calls=1, intervals=1, wall_seconds=0.5
    )

    assert budget_used(episode_dir) == (2, 2, 1.0)


def test_budget_event_journal_is_append_only(episode_dir: Path) -> None:
    append_budget_event(
        episode_dir,
        ConsultationBudgetEventV1(
            consultation_id="c-1", llm_calls=1, intervals=1,
            wall_seconds=1.5, created_at="2026-09-08T00:00:00+00:00",
        ),
    )

    assert budget_used(episode_dir) == (1, 1, 1.5)


def test_llm_call_limit_exhaustion_is_typed_422(episode_dir: Path) -> None:
    limits = ConsultationBudgetLimits(
        llm_calls_limit=1, intervals_limit=3, wall_seconds_limit=600.0
    )
    ensure_budget_available(episode_dir, limits)
    consume_budget(
        episode_dir, "c-1", llm_calls=1, intervals=1, wall_seconds=0.0
    )

    with pytest.raises(CockpitUnprocessableError) as excinfo:
        ensure_budget_available(episode_dir, limits)
    assert excinfo.value.code == "consultation-budget-exhausted"


def test_interval_limit_exhaustion_is_typed_422(episode_dir: Path) -> None:
    limits = ConsultationBudgetLimits(
        llm_calls_limit=6, intervals_limit=1, wall_seconds_limit=600.0
    )
    consume_budget(
        episode_dir, "c-1", llm_calls=1, intervals=1, wall_seconds=0.0
    )

    with pytest.raises(CockpitUnprocessableError) as excinfo:
        ensure_budget_available(episode_dir, limits)
    assert excinfo.value.code == "consultation-budget-exhausted"


def test_wall_seconds_limit_is_enforced(episode_dir: Path) -> None:
    limits = ConsultationBudgetLimits(
        llm_calls_limit=6, intervals_limit=3, wall_seconds_limit=600.0
    )
    consume_budget(
        episode_dir, "c-1", llm_calls=1, intervals=1, wall_seconds=600.0
    )

    with pytest.raises(CockpitUnprocessableError) as excinfo:
        ensure_budget_available(episode_dir, limits)
    assert excinfo.value.code == "consultation-budget-exhausted"


def test_budget_limit_config_with_defaults_fallback(
    tmp_path: Path,
) -> None:
    assert load_budget_limits(tmp_path / "missing.json") == ConsultationBudgetLimits()
    assert DEFAULT_LLM_CALLS_LIMIT == 6
    assert DEFAULT_INTERVALS_LIMIT == 3
    assert DEFAULT_WALL_SECONDS_LIMIT == 600.0

    config = tmp_path / "consultation.json"
    config.write_bytes(
        b'{"llm_calls_limit": 2, "intervals_limit": 1, "wall_seconds_limit": 30.0}'
    )
    limits = load_budget_limits(config)
    assert limits == ConsultationBudgetLimits(
        llm_calls_limit=2, intervals_limit=1, wall_seconds_limit=30.0
    )


def test_budget_limit_malformed_config_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    config = tmp_path / "consultation.json"
    config.write_bytes(b"not-json")

    assert load_budget_limits(config) == ConsultationBudgetLimits()


# ---------------------------------------------------------------------------
# API view shape (pinned consultation contract)
# ---------------------------------------------------------------------------


def test_consultation_view_matches_pinned_contract(episode_dir: Path) -> None:
    _seed_consultation(episode_dir)
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1(
            judgment_id="j-1", consultation_id="c-1", proposal_id="prop-1",
            decision="adopt",
            scope=ConsultationScope(composition=True, appearance=False, audio=False),
            note=None, created_at="2026-09-08T00:00:02+00:00",
        ),
    )
    consume_budget(
        episode_dir, "c-1", llm_calls=1, intervals=1, wall_seconds=1.25
    )

    view = consultation_view(episode_dir, _record(), ConsultationBudgetLimits())

    assert set(view) == {
        "consultation_id", "created_at", "message", "proposals", "judgments", "budget",
        "policy", "rebuild", "policy_outcomes",
    }
    assert view["message"] == "ブログ告知用に短くしたい"
    proposal = cast("list[dict[str, object]]", view["proposals"])[0]
    assert set(proposal) == {"proposal_id", "title", "summary", "details"}
    details = cast("dict[str, object]", proposal["details"])
    assert set(details) == {
        "audience_message", "structure", "duration_estimate", "candidate_scenes",
        "subtitle_policy", "audio_policy", "tempo_policy", "reference_mapping",
        "unused_reasons", "unconfirmed",
    }
    assert details["candidate_scenes"] == ["opening", "demo"]
    judgment = cast("list[dict[str, object]]", view["judgments"])[0]
    assert set(judgment) == {
        "judgment_id", "proposal_id", "decision", "scope", "note", "created_at"
    }
    assert cast("dict[str, object]", judgment["scope"]) == {
        "composition": True, "appearance": False, "audio": False
    }
    assert view["budget"] == {
        "llm_calls_used": 1, "llm_calls_limit": 6,
        "intervals_used": 1, "intervals_limit": 3,
        "wall_seconds_used": 1.25, "wall_seconds_limit": 600.0,
        "cost_display": "unmeasured",
    }
    adopted = cast("dict[str, object]", view["policy"])["adopted"]
    assert adopted is not None
    assert cast("dict[str, object]", adopted)["judgment_id"] == "j-1"
    assert cast("dict[str, object]", adopted)["structure"] == (
        cast("dict[str, object]", details)["structure"]
    )
    assert view["rebuild"] == {
        "status": "none",
        "target_version": None,
        "detail": "この方針の再生成は要求されていません。",
    }
    assert view["policy_outcomes"] == []
