"""Tests for MomentCandidateV2 — 14 types, validation, round-trip (task 22)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from services.editorial_v2.moment_models import (
    MomentCandidateV2,
    MomentSelectionProposalV2,
)

ALL_14_TYPES = [
    "speech",
    "reaction",
    "action",
    "establishing",
    "b_roll",
    "insert",
    "product_demo",
    "screen_demo",
    "ambient",
    "transition",
    "graphic",
    "still",
    "pause",
    "alternate_take",
]


def _candidate_kwargs(
    candidate_type: str = "speech",
    candidate_id: str = "cand-001",
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "source_span": {"start_frame": 10, "end_frame": 48},
        "intent": "keep",
        "rationale": "viewer value: demonstrates comparison",
        "evidence_refs": evidence_refs if evidence_refs is not None else ["ev-001"],
        "confidence": 0.82,
        "provenance": {"producer": "test-analyzer"},
    }


@pytest.mark.parametrize("candidate_type", ALL_14_TYPES)
def test_all_14_types_construct_ok_when_valid(candidate_type: str) -> None:
    kwargs = _candidate_kwargs(candidate_type=candidate_type)
    model = MomentCandidateV2.model_validate(kwargs)
    assert model.candidate_type == candidate_type
    assert model.source_span.start_frame == 10
    assert model.source_span.end_frame == 48
    assert model.lock_state == "unlocked"


def test_unknown_type_vlog_rejected() -> None:
    kwargs = _candidate_kwargs(candidate_type="vlog")
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate(kwargs)


def test_evidence_refs_empty_rejected_at_candidate_level() -> None:
    kwargs = _candidate_kwargs(evidence_refs=[])
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate(kwargs)


def test_evidence_refs_empty_rejected_via_json() -> None:
    payload = _candidate_kwargs(evidence_refs=[])
    json_str = json.dumps(payload)
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate_json(json_str)
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate(payload)


def test_proposal_round_trip_when_valid() -> None:
    c1 = MomentCandidateV2.model_validate(
        _candidate_kwargs(candidate_type="speech", candidate_id="cand-001")
    )
    c2 = MomentCandidateV2.model_validate(
        {
            "candidate_id": "cand-002",
            "candidate_type": "b_roll",
            "source_span": {"start_frame": 100, "end_frame": 150},
            "story_block_ref": "block-01",
            "intent": "optional",
            "rationale": "optional cutaway",
            "evidence_refs": ["ev-002", "ev-003"],
            "redundancy_group": "rg-1",
            "confidence": 0.55,
            "handles": {"in_frame": 2, "out_frame": 3},
            "lock_state": "locked",
            "provenance": {"producer": "vision-analyzer", "version": "1.0"},
        }
    )
    proposal = MomentSelectionProposalV2.model_validate(
        {
            "proposal_id": "prop-001",
            "episode_id": "ep-001",
            "candidates": [c1.model_dump(mode="json"), c2.model_dump(mode="json")],
        }
    )
    assert len(proposal.candidates) == 2

    dumped = proposal.model_dump(mode="json")
    reparsed = MomentSelectionProposalV2.model_validate(dumped)
    assert reparsed == proposal

    json_bytes = proposal.model_dump_json()
    reparsed_json = MomentSelectionProposalV2.model_validate_json(json_bytes)
    assert reparsed_json == proposal


def test_proposal_rejects_when_no_keep() -> None:
    cand = MomentCandidateV2.model_validate(
        {
            "candidate_id": "cand-010",
            "candidate_type": "pause",
            "source_span": {"start_frame": 0, "end_frame": 10},
            "intent": "remove",
            "rationale": "filler pause",
            "evidence_refs": ["ev-010"],
            "confidence": 0.4,
            "provenance": {"producer": "analyzer"},
        }
    )
    with pytest.raises(ValidationError):
        MomentSelectionProposalV2.model_validate(
            {
                "proposal_id": "prop-002",
                "episode_id": "ep-002",
                "candidates": [cand.model_dump(mode="json")],
            }
        )


def test_proposal_rejects_empty_candidates() -> None:
    with pytest.raises(ValidationError):
        MomentSelectionProposalV2.model_validate(
            {
                "proposal_id": "prop-003",
                "episode_id": "ep-003",
                "candidates": [],
            }
        )


def test_handles_strict_int_rejected_when_float() -> None:
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate(
            {
                "candidate_id": "cand-020",
                "candidate_type": "speech",
                "source_span": {"start_frame": 0, "end_frame": 10},
                "intent": "keep",
                "rationale": "ok",
                "evidence_refs": ["ev-020"],
                "confidence": 0.5,
                "handles": {"in_frame": 1.5, "out_frame": 2},
                "provenance": {"producer": "analyzer"},
            }
        )


def test_source_span_strict_int_rejected_when_float() -> None:
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate(
            {
                "candidate_id": "cand-021",
                "candidate_type": "speech",
                "source_span": {"start_frame": 1.5, "end_frame": 10},
                "intent": "keep",
                "rationale": "ok",
                "evidence_refs": ["ev-021"],
                "confidence": 0.5,
                "provenance": {"producer": "analyzer"},
            }
        )


def test_historical_payload_parses_with_removal_reason_none() -> None:
    """Additive compatibility: historical MomentSelection payloads (no
    removal_reason key) parse and default to None."""
    payload = _candidate_kwargs()
    assert "removal_reason" not in payload
    model = MomentCandidateV2.model_validate(payload)
    assert model.removal_reason is None


@pytest.mark.parametrize("reason", ["false_start", "exact_duplicate"])
def test_remove_candidate_carries_each_eligible_reason(reason: str) -> None:
    kwargs = _candidate_kwargs() | {"intent": "remove", "removal_reason": reason}
    model = MomentCandidateV2.model_validate(kwargs)
    assert model.removal_reason == reason


def test_unknown_removal_reason_rejected() -> None:
    kwargs = _candidate_kwargs() | {"intent": "remove", "removal_reason": "redundant"}
    with pytest.raises(ValidationError):
        MomentCandidateV2.model_validate(kwargs)


@pytest.mark.parametrize("intent", ["keep", "optional"])
def test_keep_or_optional_with_removal_reason_rejected(intent: str) -> None:
    kwargs = _candidate_kwargs() | {"intent": intent, "removal_reason": "false_start"}
    with pytest.raises(ValidationError) as excinfo:
        MomentCandidateV2.model_validate(kwargs)
    assert excinfo.value.errors()[0]["type"] == "removal_reason_intent"


def test_remove_without_reason_parses_like_historical_artifacts() -> None:
    """Historical remove candidates carry no reason; parsing stays legal —
    eligibility is enforced at the policy boundary, not at parse time."""
    kwargs = _candidate_kwargs() | {"intent": "remove"}
    model = MomentCandidateV2.model_validate(kwargs)
    assert model.removal_reason is None


def test_proposal_round_trip_preserves_removal_reason() -> None:
    keep = MomentCandidateV2.model_validate(_candidate_kwargs())
    cut = MomentCandidateV2.model_validate(
        _candidate_kwargs(candidate_id="cand-cut")
        | {
            "intent": "remove",
            "removal_reason": "exact_duplicate",
            "evidence_refs": ["ev-001", "ev-002"],
        }
    )
    proposal = MomentSelectionProposalV2.model_validate(
        {
            "proposal_id": "prop-r",
            "episode_id": "ep-001",
            "candidates": [keep.model_dump(mode="json"), cut.model_dump(mode="json")],
        }
    )
    reparsed = MomentSelectionProposalV2.model_validate_json(
        proposal.model_dump_json()
    )
    cut_back = next(c for c in reparsed.candidates if c.candidate_id == "cand-cut")
    assert cut_back.removal_reason == "exact_duplicate"
