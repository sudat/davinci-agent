from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from services.contracts.edit_plan_0c import SubtitleTextSelector0C
from services.review_command.models import (
    CandidateTarget0C,
    Confidence0C,
    CorrectSubtitleProposal0C,
    ProposalAmbiguity0C,
    RemoveSegmentProposal0C,
    parse_proposal,
)
from services.review_command.validate import ProposalValidationError, validate_proposal
from tests.review_command.support import (
    adjust_payload,
    adjust_proposal,
    approval_payload,
    approve_editorial_plan_proposal,
    approve_remaining_proposal,
    base_envelope,
    fixture_proposal,
    load_manifest,
    manifest_plan,
    remove_payload,
    with_locked_field,
)

CLEAR_FIXTURES = ("p0c-remove-clear", "p0c-span-clear", "p0c-subtitle-clear")


@pytest.mark.parametrize("fixture_id", CLEAR_FIXTURES)
def test_clear_fixture_proposal_validates_clear_and_apply_eligible(fixture_id: str) -> None:
    manifest = load_manifest(fixture_id)

    outcome = validate_proposal(manifest_plan(fixture_id), fixture_proposal(fixture_id))

    assert outcome.classification == manifest.expected.classification == "clear"
    assert outcome.required_action == "apply"
    assert outcome.needs_human is False
    assert outcome.candidate_item_ids == manifest.expected.target_candidate_item_ids
    assert outcome.conflict is None


def test_ambiguous_fixture_classifies_ambiguous_defer_needs_human() -> None:
    outcome = validate_proposal(
        manifest_plan("p0c-ambiguous-two-targets"),
        fixture_proposal("p0c-ambiguous-two-targets"),
    )

    assert outcome.classification == "ambiguous"
    assert outcome.required_action == "defer"
    assert outcome.needs_human is True
    assert outcome.candidate_item_ids == ("s1", "s2")
    assert outcome.ambiguity_reasons != ()
    assert outcome.conflict is None


def test_locked_fixture_classifies_conflict_defer() -> None:
    outcome = validate_proposal(
        manifest_plan("p0c-locked-conflict"),
        fixture_proposal("p0c-locked-conflict"),
    )

    assert outcome.classification == "conflict"
    assert outcome.required_action == "defer"
    assert outcome.needs_human is True
    assert outcome.conflict is not None
    assert outcome.conflict.target_item_id == "v2"
    assert outcome.conflict.locked_field == "span"


@pytest.mark.parametrize(
    ("fixture_id", "item_id", "locked_field"),
    [
        ("p0c-remove-clear", "v2", "order"),
        ("p0c-span-clear", "v2", "span"),
        ("p0c-subtitle-clear", "s1", "text"),
    ],
)
def test_lock_rules_per_kind(fixture_id: str, item_id: str, locked_field: str) -> None:
    plan = with_locked_field(manifest_plan(fixture_id), item_id, locked_field)

    outcome = validate_proposal(plan, fixture_proposal(fixture_id))

    assert outcome.classification == "conflict"
    assert outcome.required_action == "defer"
    assert outcome.needs_human is True
    assert outcome.conflict is not None
    assert outcome.conflict.locked_field == locked_field
    assert outcome.conflict.target_item_id == item_id


def test_operator_approval_proposals_validate_clear_apply() -> None:
    plan = manifest_plan("p0c-remove-clear")

    for proposal in (approve_remaining_proposal(), approve_editorial_plan_proposal()):
        outcome = validate_proposal(plan, proposal)
        assert outcome.classification == "clear"
        assert outcome.required_action == "apply"
        assert outcome.needs_human is False
        assert outcome.candidate_item_ids == ()
        assert outcome.conflict is None


def test_stale_base_plan_version_is_rejected() -> None:
    proposal = fixture_proposal("p0c-remove-clear")
    stale = proposal.model_copy(update={"base_plan_version": "v2"})

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal(manifest_plan("p0c-remove-clear"), stale)

    assert excinfo.value.code == "stale_plan_version"


@pytest.mark.parametrize("fixture_id", ["p0c-remove-clear", "p0c-subtitle-clear"])
def test_unresolved_target_is_rejected(fixture_id: str) -> None:
    plan = manifest_plan(fixture_id)
    if fixture_id == "p0c-remove-clear":
        proposal = RemoveSegmentProposal0C(
            proposal_id="prop-missing-target",
            command_kind="remove_segment",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=Confidence0C(num=9, den=10),
            ambiguity=ProposalAmbiguity0C(status="clear"),
            evidence=(),
            candidate_targets=(CandidateTarget0C(item_id="v99", evidence="99番目"),),
        )
    else:
        proposal = CorrectSubtitleProposal0C(
            proposal_id="prop-missing-target",
            command_kind="correct_subtitle",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=Confidence0C(num=9, den=10),
            ambiguity=ProposalAmbiguity0C(status="clear"),
            evidence=(),
            target=SubtitleTextSelector0C(kind="subtitle_text_match", text="存在しない字幕"),
            new_text="新しい字幕",
            language="ja",
        )

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal(plan, proposal)

    assert excinfo.value.code == "unresolved_target"


def test_malformed_discriminator_is_rejected() -> None:
    payload = base_envelope("prop-unknown-kind") | {"command_kind": "explode_timeline"}

    with pytest.raises(ValidationError):
        parse_proposal(json.dumps(payload))


@pytest.mark.parametrize(
    "smuggled",
    [
        {"decision": {"classification": "clear", "action": "apply"}},
        {"classification": "clear"},
        {"action": "apply"},
        {"resulting_plan_version": "v2"},
        {"decision_id": "dec-01"},
        {"target_candidate_item_ids": ["v2"]},
    ],
)
def test_decision_payload_smuggling_is_rejected(smuggled: dict[str, object]) -> None:
    payload = remove_payload() | smuggled

    with pytest.raises(ValidationError, match="decision_field_forbidden"):
        parse_proposal(json.dumps(payload))


def test_model_authored_approval_is_rejected() -> None:
    for kind in ("remaining", "editorial_plan"):
        payload = approval_payload(kind, actor="model")

        with pytest.raises(ValidationError, match="model_authored_approval"):
            parse_proposal(payload)


def test_authored_clear_claim_with_two_viable_targets_still_classifies_ambiguous() -> None:
    proposal = fixture_proposal("p0c-ambiguous-two-targets", authored_status="clear")
    assert proposal.ambiguity.status == "clear"

    outcome = validate_proposal(manifest_plan("p0c-ambiguous-two-targets"), proposal)

    assert outcome.classification == "ambiguous"
    assert outcome.required_action == "defer"
    assert outcome.needs_human is True


def test_authored_ambiguous_claim_on_clear_target_still_classifies_clear() -> None:
    proposal = fixture_proposal("p0c-remove-clear", authored_status="ambiguous")

    outcome = validate_proposal(manifest_plan("p0c-remove-clear"), proposal)

    assert outcome.classification == "clear"
    assert outcome.required_action == "apply"
    assert outcome.needs_human is False


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        (150.5, 240, "integer"),
        (-1, 240, "greater than or equal to 0"),
        (240, 150, "span_inverted"),
        (150, 150, "span_empty"),
    ],
)
def test_malformed_span_bounds_are_rejected(start: object, end: object, code: str) -> None:
    payload = adjust_payload(start=start, end=end)

    with pytest.raises(ValidationError, match=code):
        parse_proposal(json.dumps(payload))


def test_out_of_bounds_span_is_rejected() -> None:
    proposal = adjust_proposal(start=150, end=751)

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal(manifest_plan("p0c-span-clear"), proposal)

    assert excinfo.value.code == "out_of_bounds"


def test_overflow_span_is_rejected() -> None:
    proposal = adjust_proposal(start=0, end=1 << 63)

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposal(manifest_plan("p0c-span-clear"), proposal)

    assert excinfo.value.code == "out_of_bounds"


@pytest.mark.parametrize(
    "confidence",
    [{"num": 0.9, "den": 1}, {"num": 11, "den": 10}, {"num": 1, "den": 0}],
)
def test_confidence_float_and_range_are_rejected(confidence: dict[str, object]) -> None:
    payload = remove_payload()
    payload["confidence"] = confidence

    with pytest.raises(ValidationError):
        parse_proposal(json.dumps(payload))


@pytest.mark.parametrize(
    ("status", "reasons"),
    [("ambiguous", []), ("clear", ["reason"])],
)
def test_authored_ambiguity_reason_rules(status: str, reasons: list[str]) -> None:
    payload = remove_payload()
    payload["ambiguity"] = {"status": status, "reasons": reasons}

    with pytest.raises(ValidationError, match="ambiguity_reasons"):
        parse_proposal(json.dumps(payload))
