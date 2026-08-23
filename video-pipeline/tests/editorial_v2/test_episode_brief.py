from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.editorial_v2.episode_brief import (
    EpisodeBriefNotApprovedError,
    EpisodeBriefTransitionError,
    EpisodeBriefV1,
    approve,
    propose,
    require_approved,
)


def _valid_payload(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "episode_id": "ep-brief-01",
        "audience_hypothesis": "初心者向けに旅の魅力を短く伝える",
        "viewer_promise": "3分で次の旅先が決まる",
        "episode_objective": "保存率を上げる",
        "must_include": [{"idea_or_moment": "朝市の賑わい", "note": "必ず入れる"}],
        "must_not_misrepresent": ["営業時間を誤らない"],
        "target_duration_minutes": {"min": 3, "max": 6},
        "pacing_target": "moderate",
        "editing_intensity": "standard",
        "required_assets": ["op-01"],
        "publication_constraints": ["no-spoiler"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# (a) full brief round-trip (stale_state probe: dump → revalidate preserves equality)
# ---------------------------------------------------------------------------


def test_round_trip_when_full_brief_is_valid() -> None:
    brief = EpisodeBriefV1.model_validate(_valid_payload())

    dumped = brief.model_dump(mode="json", by_alias=True)
    reparsed = EpisodeBriefV1.model_validate(dumped)
    assert reparsed == brief

    json_bytes = brief.model_dump_json(by_alias=True)
    reparsed_json = EpisodeBriefV1.model_validate_json(json_bytes)
    assert reparsed_json == brief

    # list fields accepted as list (coerced to tuple) and preserved as tuple
    assert isinstance(brief.must_include, tuple)
    assert isinstance(brief.must_not_misrepresent, tuple)
    # optional cta omitted → None
    assert brief.cta is None
    # with cta
    with_cta = EpisodeBriefV1.model_validate(_valid_payload(cta="概要欄へ"))
    assert with_cta.cta == "概要欄へ"
    assert (
        EpisodeBriefV1.model_validate(with_cta.model_dump(mode="json", by_alias=True)) == with_cta
    )


def test_round_trip_with_lists_coerced_to_tuples() -> None:
    payload = _valid_payload(
        must_include=[{"idea_or_moment": "m1"}, {"idea_or_moment": "m2", "note": "n2"}],
        must_not_misrepresent=["a", "b"],
        required_assets=["asset-a", "asset-b"],
        publication_constraints=["c1"],
    )
    brief = EpisodeBriefV1.model_validate(payload)
    assert len(brief.must_include) == 2
    assert brief.must_include[1].note == "n2"
    # dump and reload preserves tuples via coercion
    dumped = brief.model_dump(mode="json")
    assert isinstance(dumped["must_include"], list)
    assert EpisodeBriefV1.model_validate(dumped) == brief


# ---------------------------------------------------------------------------
# (b) missing any required field → ValidationError (malformed_input probe)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "omit",
    [
        "audience_hypothesis",
        "viewer_promise",
        "episode_objective",
        "must_include",
        "must_not_misrepresent",
        "target_duration_minutes",
        "pacing_target",
        "editing_intensity",
    ],
)
def test_missing_required_field_raises_validation_error(omit: str) -> None:
    payload = _valid_payload()
    payload.pop(omit)
    with pytest.raises(ValidationError):
        EpisodeBriefV1.model_validate(payload)


def test_empty_must_include_rejected() -> None:
    with pytest.raises(ValidationError):
        EpisodeBriefV1.model_validate(_valid_payload(must_include=[]))


def test_empty_must_not_misrepresent_rejected() -> None:
    with pytest.raises(ValidationError):
        EpisodeBriefV1.model_validate(_valid_payload(must_not_misrepresent=[]))


def test_duration_bounds_inverted_rejected() -> None:
    with pytest.raises(ValidationError):
        EpisodeBriefV1.model_validate(
            _valid_payload(target_duration_minutes={"min": 10, "max": 3})
        )


def test_duration_bounds_equal_ok() -> None:
    brief = EpisodeBriefV1.model_validate(
        _valid_payload(target_duration_minutes={"min": 5, "max": 5})
    )
    assert brief.target_duration_minutes.min == 5


# ---------------------------------------------------------------------------
# (c) transition matrix
# ---------------------------------------------------------------------------


def test_transition_draft_to_proposed_ok() -> None:
    brief = EpisodeBriefV1.model_validate(_valid_payload(status="draft"))
    assert brief.status == "draft"
    proposed = propose(brief)
    assert proposed.status == "proposed"
    assert proposed.episode_id == brief.episode_id
    # original unchanged (frozen)
    assert brief.status == "draft"


def test_transition_proposed_to_approved_ok_with_approval_ref() -> None:
    brief = EpisodeBriefV1.model_validate(_valid_payload(status="draft"))
    proposed = propose(brief)
    approved = approve(proposed)
    assert approved.status == "approved"
    assert approved.approval_ref is not None
    assert approved.approval_ref.purpose == "editorial"
    assert approved.approval_ref.target_type == "episode-brief"
    assert approved.approval_ref.decision == "approve"
    # target_hash is valid sha256 (64 hex)
    assert len(approved.approval_ref.target_hash) == 64
    assert all(c in "0123456789abcdef" for c in approved.approval_ref.target_hash)
    # record_id shape compatible with approvals (opr-...)
    assert approved.approval_ref.record_id.startswith("opr-")
    # approved round-trips
    assert (
        EpisodeBriefV1.model_validate(approved.model_dump(mode="json", by_alias=True))
        == approved
    )


def test_approve_with_explicit_record_id_and_actor() -> None:
    brief = propose(EpisodeBriefV1.model_validate(_valid_payload()))
    approved = approve(brief, record_id="opr-00000001", actor_id="op-01")
    assert approved.approval_ref is not None
    assert approved.approval_ref.record_id == "opr-00000001"
    assert approved.approval_ref.actor_id == "op-01"


def test_transition_draft_to_approved_typed_error() -> None:
    brief = EpisodeBriefV1.model_validate(_valid_payload(status="draft"))
    with pytest.raises(EpisodeBriefTransitionError) as exc:
        approve(brief)
    assert exc.value.code == "illegal-transition"
    # also propose on non-draft is illegal
    proposed = propose(brief)
    with pytest.raises(EpisodeBriefTransitionError):
        propose(proposed)


def test_approved_again_typed_error() -> None:
    brief = EpisodeBriefV1.model_validate(_valid_payload())
    approved = approve(propose(brief))
    with pytest.raises(EpisodeBriefTransitionError):
        approve(approved)


def test_approved_without_ref_rejected_by_schema() -> None:
    # Direct construction of approved without approval_ref must fail validation
    payload = _valid_payload(status="approved")
    with pytest.raises(ValidationError) as exc:
        EpisodeBriefV1.model_validate(payload)
    assert "approval_ref" in str(exc.value).lower()


def test_non_approved_with_ref_rejected() -> None:
    # draft/proposed carrying approval_ref is invalid
    brief = EpisodeBriefV1.model_validate(_valid_payload(status="draft"))
    proposed = propose(brief)
    approved = approve(proposed)
    assert approved.approval_ref is not None
    ref_payload = approved.approval_ref.model_dump()
    # try to carry ref on draft
    with pytest.raises(ValidationError):
        EpisodeBriefV1.model_validate(
            {**_valid_payload(status="draft"), "approval_ref": ref_payload}
        )


# ---------------------------------------------------------------------------
# (d) require_approved gate
# ---------------------------------------------------------------------------


def test_require_approved_on_non_approved_typed_error() -> None:
    draft = EpisodeBriefV1.model_validate(_valid_payload(status="draft"))
    with pytest.raises(EpisodeBriefNotApprovedError) as exc:
        require_approved(draft)
    assert exc.value.code == "not-approved"

    proposed = propose(draft)
    with pytest.raises(EpisodeBriefNotApprovedError):
        require_approved(proposed)


def test_require_approved_on_approved_ok() -> None:
    brief = EpisodeBriefV1.model_validate(_valid_payload())
    approved = approve(propose(brief))
    result = require_approved(approved)
    assert result == approved
