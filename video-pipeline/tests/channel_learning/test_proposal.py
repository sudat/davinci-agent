"""Task 57: governed profile change proposals + holdout (TDD).

Covers:
(a) single correction -> no proposal (None)
(b) repeated approved same-statement corrections -> proposal with repeated_evidence
(c) explicit_request single evidence -> proposal
(d) approve -> applied -> holdout round flow with version bump record
(e) separation: proposal/holdout contain no reference_learning import, observation untouched
(f) one-off audience metric -> no proposal

Adversarial probes:
- malformed_input: bad status transition
- stale_state: holdout/profile round-trip
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from services.channel_learning.holdout import (
    HoldoutEvaluationV1,
    ProfileVersionBumpV1,
    apply_proposal,
    approve_proposal,
    evaluate_holdout,
)
from services.channel_learning.proposal import (
    ChannelProfileChangeProposalV1,
    ChannelProfileDomain,
    EvidenceClass,
    EvidenceRecord,
    maybe_generate_proposal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approved_edit(
    *,
    statement: str = "use fewer punch-ins",
    domain: ChannelProfileDomain = ChannelProfileDomain.pacing,
    evidence_ref: str = "ev-0001",
    evidence_class: EvidenceClass = EvidenceClass.approved_edit,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_ref,
        evidence_class=evidence_class,
        domain=domain,
        statement=statement,
        evidence_ref=evidence_ref,
        approved=True,
    )


def _audience_outcome(
    *,
    statement: str = "CTR 0.02 on episode 42",
    domain: ChannelProfileDomain = ChannelProfileDomain.pacing,
    evidence_ref: str = "obs-0001",
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_ref,
        evidence_class=EvidenceClass.audience_outcome,
        domain=domain,
        statement=statement,
        evidence_ref=evidence_ref,
        approved=True,
    )


# ---------------------------------------------------------------------------
# (a) single correction -> None
# ---------------------------------------------------------------------------


def test_single_correction_no_proposal() -> None:
    records = [_approved_edit(evidence_ref="ev-0001")]

    proposal = maybe_generate_proposal(records)

    assert proposal is None


# ---------------------------------------------------------------------------
# (b) repeated approved same-statement -> proposal with repeated_evidence
# ---------------------------------------------------------------------------


def test_repeated_corrections_generate_proposal() -> None:
    stmt = "use fewer punch-ins"
    records = [
        _approved_edit(evidence_ref="ev-0001", statement=stmt),
        _approved_edit(evidence_ref="ev-0002", statement=stmt),
    ]

    proposal = maybe_generate_proposal(records)

    assert proposal is not None
    assert proposal.origin == "repeated_evidence"
    assert proposal.statement == stmt
    assert proposal.evidence_class == EvidenceClass.approved_edit
    assert proposal.domain == ChannelProfileDomain.pacing
    assert len(proposal.supporting_evidence) == 2
    assert proposal.status == "draft"
    # threshold param default 2 — explicit check
    assert proposal.evidence_count == 2


def test_repeated_threshold_custom() -> None:
    stmt = "subtitle should be concise"
    records = [
        _approved_edit(evidence_ref="ev-0001", statement=stmt),
        _approved_edit(evidence_ref="ev-0002", statement=stmt),
        _approved_edit(evidence_ref="ev-0003", statement=stmt),
    ]

    # threshold 3 -> need 3
    assert maybe_generate_proposal(records, threshold=3) is not None
    assert maybe_generate_proposal(records[:2], threshold=3) is None


def test_repeated_different_statement_no_proposal() -> None:
    records = [
        _approved_edit(evidence_ref="ev-0001", statement="use fewer punch-ins"),
        _approved_edit(evidence_ref="ev-0002", statement="leave reactions longer"),
    ]

    proposal = maybe_generate_proposal(records)

    assert proposal is None


def test_repeated_unapproved_not_counted() -> None:
    stmt = "use fewer punch-ins"
    records = [
        EvidenceRecord(
            evidence_id="ev-0001",
            evidence_class=EvidenceClass.approved_edit,
            domain=ChannelProfileDomain.pacing,
            statement=stmt,
            evidence_ref="ev-0001",
            approved=False,
        ),
        EvidenceRecord(
            evidence_id="ev-0002",
            evidence_class=EvidenceClass.approved_edit,
            domain=ChannelProfileDomain.pacing,
            statement=stmt,
            evidence_ref="ev-0002",
            approved=False,
        ),
    ]

    proposal = maybe_generate_proposal(records)

    assert proposal is None


# ---------------------------------------------------------------------------
# (c) explicit_request single evidence -> proposal
# ---------------------------------------------------------------------------


def test_explicit_request_single_generates() -> None:
    records = [_approved_edit(evidence_ref="ev-0001")]

    proposal = maybe_generate_proposal(records, explicit_request=True)

    assert proposal is not None
    assert proposal.origin == "explicit_request"
    assert proposal.status == "draft"
    assert len(proposal.supporting_evidence) == 1


def test_explicit_request_empty_returns_none() -> None:
    proposal = maybe_generate_proposal([], explicit_request=True)

    assert proposal is None


# ---------------------------------------------------------------------------
# (d) approve -> applied -> holdout round flow with version bump record
# ---------------------------------------------------------------------------


def test_approve_applied_holdout_flow() -> None:
    stmt = "keep intros under 15s"
    records = [
        EvidenceRecord(
            evidence_id="ev-0001",
            evidence_class=EvidenceClass.explicit_rule,
            domain=ChannelProfileDomain.story_structure,
            statement=stmt,
            evidence_ref="ev-0001",
            approved=True,
        ),
        EvidenceRecord(
            evidence_id="ev-0002",
            evidence_class=EvidenceClass.explicit_rule,
            domain=ChannelProfileDomain.story_structure,
            statement=stmt,
            evidence_ref="ev-0002",
            approved=True,
        ),
    ]

    proposal = maybe_generate_proposal(records)
    assert proposal is not None
    assert proposal.status == "draft"

    approved = approve_proposal(proposal)
    assert approved.status == "approved"
    # original unchanged (frozen)
    assert proposal.status == "draft"

    # bump record on apply
    applied, bump = apply_proposal(approved, current_version=7, bump_id="bump-0001")

    assert applied.status == "applied"
    assert isinstance(bump, ProfileVersionBumpV1)
    assert bump.proposal_id == proposal.proposal_id
    assert bump.from_version == 7
    assert bump.to_version == 8

    # stale_state round-trip
    bump_rt = ProfileVersionBumpV1.model_validate(bump.model_dump(mode="json"))
    assert bump_rt == bump

    applied_rt = ChannelProfileChangeProposalV1.model_validate(
        applied.model_dump(mode="json")
    )
    assert applied_rt == applied

    # holdout evaluation compares before/after
    evaluation = evaluate_holdout(
        applied,
        bump,
        before_metric=0.032,
        after_metric=0.041,
        evaluation_id="eval-0001",
    )

    assert isinstance(evaluation, HoldoutEvaluationV1)
    assert evaluation.proposal_id == proposal.proposal_id
    assert evaluation.profile_version_before == 7
    assert evaluation.profile_version_after == 8
    assert evaluation.before_metric == pytest.approx(0.032)
    assert evaluation.after_metric == pytest.approx(0.041)
    assert evaluation.delta == pytest.approx(0.009)

    eval_rt = HoldoutEvaluationV1.model_validate(evaluation.model_dump(mode="json"))
    assert eval_rt == evaluation


def test_malformed_status_transition_rejected() -> None:
    stmt = "use fewer punch-ins"
    records = [
        _approved_edit(evidence_ref="ev-0001", statement=stmt),
        _approved_edit(evidence_ref="ev-0002", statement=stmt),
    ]
    proposal = maybe_generate_proposal(records)
    assert proposal is not None

    # cannot apply directly from draft
    with pytest.raises((ValidationError, ValueError)):
        apply_proposal(proposal, current_version=1)

    # approve then reject is invalid
    approved = approve_proposal(proposal)
    with pytest.raises((ValidationError, ValueError)):
        # trying to approve again
        approve_proposal(approved)

    # cannot evaluate holdout on draft
    bump = ProfileVersionBumpV1(
        bump_id="bump-0002",
        proposal_id=proposal.proposal_id,
        from_version=1,
        to_version=2,
    )
    with pytest.raises((ValidationError, ValueError)):
        evaluate_holdout(proposal, bump, before_metric=0.01, after_metric=0.02)


def test_evidence_class_distinguishable() -> None:
    # four streams must stay as field, not comment
    assert {e.value for e in EvidenceClass} == {
        "explicit_rule",
        "reference_taste",
        "approved_edit",
        "audience_outcome",
    }
    # proposal preserves class
    rec = _approved_edit(evidence_class=EvidenceClass.reference_taste)
    second = rec.model_copy(update={"evidence_id": "ev-0002", "evidence_ref": "ev-0002"})
    proposal = maybe_generate_proposal([rec, second])
    assert proposal is not None
    assert proposal.evidence_class == EvidenceClass.reference_taste


# ---------------------------------------------------------------------------
# (e) separation: no cross-import
# ---------------------------------------------------------------------------


def test_separation_no_cross_import() -> None:
    import services.channel_learning.holdout as holdout_mod  # noqa: PLC0415
    import services.channel_learning.proposal as proposal_mod  # noqa: PLC0415

    proposal_source = pathlib.Path(proposal_mod.__file__).read_text(encoding="utf-8")
    holdout_source = pathlib.Path(holdout_mod.__file__).read_text(encoding="utf-8")

    # No cross-import of reference_learning models (import statement, not doc mention)
    assert "from services.reference_learning" not in proposal_source
    assert "import services.reference_learning" not in proposal_source
    assert "from services.reference_learning" not in holdout_source
    assert "import services.reference_learning" not in holdout_source
    # No import of observation models into preference/holdout logic
    assert "from services.channel_learning.observation import" not in proposal_source
    assert "from services.channel_learning.observation import" not in holdout_source
    assert "import services.channel_learning.observation" not in proposal_source
    assert "import services.channel_learning.observation" not in holdout_source
    # observation.py untouched — must not import proposal/holdout symbols
    obs_source = pathlib.Path("services/channel_learning/observation.py").read_text(
        encoding="utf-8"
    )
    assert "from services.channel_learning.proposal" not in obs_source
    assert "from services.channel_learning.holdout" not in obs_source
    # docstring must document separation
    assert "separation" in (proposal_mod.__doc__ or "").lower()
    assert "separation" in (holdout_mod.__doc__ or "").lower()


# ---------------------------------------------------------------------------
# (f) one-off audience metric -> no proposal
# ---------------------------------------------------------------------------


def test_single_audience_metric_no_proposal() -> None:
    records = [_audience_outcome(evidence_ref="obs-0001")]

    proposal = maybe_generate_proposal(records)

    assert proposal is None

    # even with different domain, still single -> none
    records2 = [_audience_outcome(evidence_ref="obs-0002", domain=ChannelProfileDomain.color)]

    assert maybe_generate_proposal(records2) is None


def test_single_audience_metric_explicit_does_generate() -> None:
    # explicit_request overrides single rule — proves origin distinction
    records = [_audience_outcome(evidence_ref="obs-0001")]

    proposal = maybe_generate_proposal(records, explicit_request=True)

    assert proposal is not None
    assert proposal.origin == "explicit_request"
    assert proposal.evidence_class == EvidenceClass.audience_outcome
