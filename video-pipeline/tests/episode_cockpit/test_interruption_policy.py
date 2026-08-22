"""Task 48: typed three-level interruption policy (PRD 13.2.1).

The 6-injection acceptance scenario: the four routine kinds
(subtitle path fallback, transient MCP failure, optional effect
unavailable, low-confidence B-roll) continue automatically with a
notice and a serializable record; only rights ambiguity and
publication approval block the operator. Unknown interruption kinds
fail closed to HUMAN_DECISION_REQUIRED instead of guessing.
"""

from __future__ import annotations

import pytest

from services.episode_cockpit.interruption_policy import (
    InterruptionDecision,
    InterruptionEvent,
    InterruptionSummary,
    aggregate_decisions,
    classify_interruption,
)

SAFE_KINDS = (
    "subtitle_path_fallback",
    "mcp_transient_failure",
    "optional_effect_unavailable",
    "low_confidence_b_roll",
)
BLOCKING_KINDS = ("rights_ambiguity", "publication_approval")
ALL_KINDS = SAFE_KINDS + BLOCKING_KINDS


# ---------------------------------------------------------------------------
# (a) the four safe kinds auto-resolve: continue, notice, no operator block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", SAFE_KINDS)
def test_safe_kind_auto_resolves_without_blocking(kind: str) -> None:
    decision = classify_interruption(InterruptionEvent(kind=kind))

    assert decision.level == "SAFE_AUTO_RESOLVE"
    assert decision.blocks_operator is False
    assert decision.notice_text != ""
    assert decision.record.level == "SAFE_AUTO_RESOLVE"


# ---------------------------------------------------------------------------
# (b) rights ambiguity stops for a human
# ---------------------------------------------------------------------------


def test_rights_ambiguity_requires_human_and_blocks() -> None:
    decision = classify_interruption(InterruptionEvent(kind="rights_ambiguity"))

    assert decision.level == "HUMAN_DECISION_REQUIRED"
    assert decision.blocks_operator is True


# ---------------------------------------------------------------------------
# (c) publication approval stops for a human
# ---------------------------------------------------------------------------


def test_publication_approval_requires_human_and_blocks() -> None:
    decision = classify_interruption(InterruptionEvent(kind="publication_approval"))

    assert decision.level == "HUMAN_DECISION_REQUIRED"
    assert decision.blocks_operator is True


# ---------------------------------------------------------------------------
# (d) exactly the two blocking kinds block (all six kinds parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_blocks_operator_only_for_blocking_kinds(kind: str) -> None:
    decision = classify_interruption(InterruptionEvent(kind=kind))

    assert decision.blocks_operator == (kind in BLOCKING_KINDS)


# ---------------------------------------------------------------------------
# (e) unknown kind fails closed to HUMAN
# ---------------------------------------------------------------------------


def test_unknown_kind_fails_closed_to_human_decision() -> None:
    decision = classify_interruption(InterruptionEvent(kind="mystery_failure"))

    assert decision.level == "HUMAN_DECISION_REQUIRED"
    assert decision.blocks_operator is True
    assert decision.notice_text != ""


# ---------------------------------------------------------------------------
# (f) aggregate counts by level, blocking count, notices
# ---------------------------------------------------------------------------


def test_aggregate_counts_levels_blocking_and_notices() -> None:
    decisions = [
        classify_interruption(InterruptionEvent(kind=kind))
        for kind in (*ALL_KINDS, "mystery_failure")
    ]

    summary = aggregate_decisions(decisions)

    assert summary.counts == {
        "SAFE_AUTO_RESOLVE": 4,
        "DEGRADED_BUT_RECOVERABLE": 0,
        "HUMAN_DECISION_REQUIRED": 3,
    }
    assert summary.blocking_count == 3
    assert summary.notices == tuple(decision.notice_text for decision in decisions)


def test_aggregate_of_no_decisions_is_all_zero() -> None:
    summary = aggregate_decisions([])

    assert summary.counts == {
        "SAFE_AUTO_RESOLVE": 0,
        "DEGRADED_BUT_RECOVERABLE": 0,
        "HUMAN_DECISION_REQUIRED": 0,
    }
    assert summary.blocking_count == 0
    assert summary.notices == ()


# ---------------------------------------------------------------------------
# (g) the decision and its record survive a JSON round trip (stale state)
# ---------------------------------------------------------------------------


def test_decision_and_record_survive_json_round_trip() -> None:
    decision = classify_interruption(
        InterruptionEvent(
            kind="subtitle_path_fallback",
            episode_id="ep-001",
            stage="subtitle",
            detail="primary implementation unavailable",
        )
    )

    restored = InterruptionDecision.model_validate(decision.model_dump(mode="json"))

    assert restored == decision
    assert restored.record.episode_id == "ep-001"
    assert restored.record.stage == "subtitle"
    assert restored.record.detail == "primary implementation unavailable"
    assert restored.record.resolution == decision.record.resolution
    assert restored.blocks_operator is False


def test_summary_survives_json_round_trip() -> None:
    decisions = [classify_interruption(InterruptionEvent(kind=kind)) for kind in ALL_KINDS]
    summary = aggregate_decisions(decisions)

    restored = InterruptionSummary.model_validate(summary.model_dump(mode="json"))

    assert restored == summary
    assert restored.blocking_count == 2
    assert restored.notices == tuple(decision.notice_text for decision in decisions)
