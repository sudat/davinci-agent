"""Task 29 — moment-selection proposal validation + commit path + event compat.

GWT coverage:
(a) valid proposal → validation ok → commit → receipt + event appended + version bump
(b) hallucinated evidence ref / span → typed HallucinatedReferenceError naming it, no commit
(c) lock conflict → typed LockConflictError refusal, no commit
(d) idempotent re-commit → typed DuplicateCommitError, log/index unchanged
(e) v1 review_command event flows still round-trip alongside the new v2 kind
(f) committed events round-trip through the store and parse back to the proposal
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from services.editorial_v2.evidence_v2 import assemble_evidence_v2
from services.editorial_v2.moment_models import (
    MomentCandidateType,
    MomentCandidateV2,
    MomentIntent,
    MomentProvenance,
    MomentSelectionProposalV2,
    MomentSourceSpan,
)
from services.editorial_v2.proposal_validate import (
    CommitReceipt,
    DuplicateCommitError,
    EpisodeMismatchError,
    EvidenceCoverageError,
    HallucinatedReferenceError,
    LockConflictError,
    MomentLockRecord,
    MomentSelectionStore,
    ValidationMismatchError,
    ValidationResult,
    commit_selection,
    initialize_moment_store,
    load_moment_index,
    validate_proposal,
)
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    MOMENT_SELECTION_V2_COMMITTED,
    EventStreamError,
    build_event,
    event_proposal,
)
from services.review_command.models import (
    CandidateTarget0C,
    Confidence0C,
    ProposalAmbiguity0C,
    RemoveSegmentProposal0C,
)
from services.review_command.store import append_events, load_events
from tests.editorial_v2.fixtures.three_pass_fixture import (
    EPISODE_ID,
    SOURCE_ID,
    make_episode_artifact,
    open_api,
)


def _candidate(  # noqa: PLR0913 (fixture DSL: one kwarg per decision field)
    cid: str,
    ctype: MomentCandidateType,
    start: int,
    end: int,
    *,
    intent: MomentIntent,
    refs: tuple[str, ...],
) -> MomentCandidateV2:
    return MomentCandidateV2(
        candidate_id=cid,
        candidate_type=ctype,
        source_span=MomentSourceSpan(start_frame=start, end_frame=end),
        intent=intent,
        rationale=f"{cid} decision for the w3 episode",
        evidence_refs=refs,
        confidence=0.9,
        provenance=MomentProvenance(producer="test-suite", version="v1"),
    )


def _valid_candidates() -> tuple[MomentCandidateV2, ...]:
    return (
        _candidate("cand-a", "speech", 0, 100, intent="keep", refs=("shot-a",)),
        _candidate("cand-b", "reaction", 100, 200, intent="keep", refs=("shot-b",)),
        _candidate("cand-e", "speech", 500, 600, intent="remove", refs=("shot-e",)),
    )


def _proposal(
    candidates: tuple[MomentCandidateV2, ...], *, proposal_id: str = "prop-moment-1"
) -> MomentSelectionProposalV2:
    return MomentSelectionProposalV2(
        proposal_id=proposal_id, episode_id=EPISODE_ID, candidates=candidates
    )


def _store(tmp_path: Path) -> MomentSelectionStore:
    store = MomentSelectionStore(plan_dir=tmp_path / "moment-store")
    initialize_moment_store(store, episode_id=EPISODE_ID)
    return store


def test_valid_proposal_commits_appends_event_and_bumps_version(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        candidates = _valid_candidates()
        bundle = assemble_evidence_v2(api, candidates, source_id=SOURCE_ID)
        proposal = _proposal(candidates)

        result = validate_proposal(proposal, api, bundle)

        assert isinstance(result, ValidationResult)
        assert result.ok is True
        assert result.candidates_checked == 3
        assert result.refs_verified == 3

        store = _store(tmp_path)
        receipt = commit_selection(proposal, result, store=store)

        assert isinstance(receipt, CommitReceipt)
        assert receipt.version == 2
        assert receipt.base_version == "v1"
        assert receipt.proposal_sha256 == result.proposal_sha256

        events = load_events(store.log_path)
        assert len(events) == 1
        assert events[0].kind == MOMENT_SELECTION_V2_COMMITTED
        assert events[0].kind == "moment-selection-v2-committed"
        assert events[0].base_plan_version == "v1"
        assert events[0].result_plan_version == "v2"
        assert events[0].proposal_sha256 == receipt.proposal_sha256
        assert events[0].event_id == receipt.event_id

        assert set(load_moment_index(store).versions) == {"1", "2"}

        second_candidates = (
            *_valid_candidates()[:2],
            _candidate("cand-g", "b_roll", 600, 610, intent="optional", refs=("shot-g",)),
        )
        second = _proposal(second_candidates, proposal_id="prop-moment-2")
        second_bundle = assemble_evidence_v2(api, second_candidates, source_id=SOURCE_ID)
        second_receipt = commit_selection(
            second, validate_proposal(second, api, second_bundle), store=store
        )

        assert second_receipt.version == 3
        assert second_receipt.base_version == "v2"
        assert len(load_events(store.log_path)) == 2
        assert set(load_moment_index(store).versions) == {"1", "2", "3"}


def test_hallucinated_evidence_ref_named_and_nothing_committed(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        bundle = assemble_evidence_v2(api, _valid_candidates(), source_id=SOURCE_ID)
        poisoned = (
            *_valid_candidates()[:2],
            _candidate(
                "cand-e",
                "speech",
                500,
                600,
                intent="remove",
                refs=("shot-hallucinated-999",),
            ),
        )

        with pytest.raises(HallucinatedReferenceError) as excinfo:
            validate_proposal(_proposal(poisoned), api, bundle)

        assert excinfo.value.reference == "shot-hallucinated-999"
        assert "shot-hallucinated-999" in str(excinfo.value)

        # the refused proposal leaves no trace: the next valid commit is still v2
        store = _store(tmp_path)
        clean = _proposal(_valid_candidates())
        receipt = commit_selection(clean, validate_proposal(clean, api, bundle), store=store)
        assert receipt.version == 2
        assert len(load_events(store.log_path)) == 1


def test_hallucinated_span_named_and_refused(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        base = (
            *_valid_candidates()[:2],
            _candidate("cand-span", "speech", 500, 600, intent="remove", refs=("shot-e",)),
        )
        bundle = assemble_evidence_v2(api, base, source_id=SOURCE_ID)
        poisoned = (
            *_valid_candidates()[:2],
            _candidate("cand-span", "speech", 605, 700, intent="remove", refs=("shot-e",)),
        )

        with pytest.raises(HallucinatedReferenceError) as excinfo:
            validate_proposal(_proposal(poisoned), api, bundle)

        assert excinfo.value.reference == "cand-span"
        assert "605" in str(excinfo.value)
        assert "700" in str(excinfo.value)


def test_bundle_gap_is_refused_before_any_requery(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        bundle = assemble_evidence_v2(api, _valid_candidates()[:2], source_id=SOURCE_ID)

        with pytest.raises(EvidenceCoverageError) as excinfo:
            validate_proposal(_proposal(_valid_candidates()), api, bundle)

        assert "cand-e" in str(excinfo.value)


def test_duplicate_candidate_ids_are_refused(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        twins = (
            _candidate("cand-a", "speech", 0, 100, intent="keep", refs=("shot-a",)),
            _candidate("cand-a", "speech", 0, 100, intent="keep", refs=("shot-a",)),
        )

        with pytest.raises(EvidenceCoverageError) as excinfo:
            validate_proposal(
                _proposal(twins), api, assemble_evidence_v2(api, twins, source_id=SOURCE_ID)
            )

        assert "cand-a" in str(excinfo.value)


def test_lock_conflict_refuses_and_nothing_commits(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        candidates = _valid_candidates()
        bundle = assemble_evidence_v2(api, candidates, source_id=SOURCE_ID)
        locks = (
            MomentLockRecord(
                candidate_id="cand-a",
                locked_intent="keep",
                locked_by="operator-1",
                locked_at="2026-08-23T00:00:00Z",
            ),
        )

        agreeing = validate_proposal(_proposal(candidates), api, bundle, locks=locks)
        assert agreeing.locks_checked == 1

        conflicting = _proposal(
            (
                _candidate("cand-a", "speech", 0, 100, intent="remove", refs=("shot-a",)),
                *candidates[1:],
            )
        )
        with pytest.raises(LockConflictError) as excinfo:
            validate_proposal(conflicting, api, bundle, locks=locks)

        assert excinfo.value.candidate_id == "cand-a"
        assert excinfo.value.locked_intent == "keep"
        assert excinfo.value.incoming_intent == "remove"

        store = _store(tmp_path)
        assert load_events(store.log_path) == ()
        assert set(load_moment_index(store).versions) == {"1"}


def test_idempotent_recommit_refused_as_duplicate(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        proposal = _proposal(_valid_candidates())
        result = validate_proposal(
            proposal, api, assemble_evidence_v2(api, proposal.candidates, source_id=SOURCE_ID)
        )
        store = _store(tmp_path)
        commit_selection(proposal, result, store=store)

        with pytest.raises(DuplicateCommitError) as excinfo:
            commit_selection(proposal, result, store=store)

        assert result.proposal_sha256 in str(excinfo.value)
        assert len(load_events(store.log_path)) == 1
        assert set(load_moment_index(store).versions) == {"1", "2"}


def test_committed_event_round_trips_through_the_store(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        proposal = _proposal(_valid_candidates())
        result = validate_proposal(
            proposal, api, assemble_evidence_v2(api, proposal.candidates, source_id=SOURCE_ID)
        )
        store = _store(tmp_path)
        commit_selection(proposal, result, store=store)

        events = load_events(store.log_path)
        committed = events[-1]

        assert (
            committed.proposal_sha256
            == hashlib.sha256(committed.proposal_json.encode()).hexdigest()
        )
        parsed = MomentSelectionProposalV2.model_validate_json(committed.proposal_json)
        assert parsed == proposal


def test_v1_and_v2_events_round_trip_in_one_stream(tmp_path: Path) -> None:
    v1_event = build_event(
        sequence=1,
        kind="proposal_recorded",
        proposal=RemoveSegmentProposal0C(
            proposal_id="prop-0c-1",
            command_kind="remove_segment",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=Confidence0C(num=9, den=10),
            ambiguity=ProposalAmbiguity0C(status="clear"),
            evidence=(),
            candidate_targets=(CandidateTarget0C(item_id="v2", evidence="冗長カット"),),
        ),
        base_plan_version="v1",
        previous_event_hash=GENESIS_EVENT_HASH,
        actor_intent="model",
    )
    store = _store(tmp_path)
    append_events(store.log_path, (v1_event,))

    with open_api(make_episode_artifact(), tmp_path) as api:
        proposal = _proposal(_valid_candidates())
        result = validate_proposal(
            proposal, api, assemble_evidence_v2(api, proposal.candidates, source_id=SOURCE_ID)
        )
        receipt = commit_selection(proposal, result, store=store)

    events = load_events(store.log_path)
    assert [event.kind for event in events] == [
        "proposal_recorded",
        MOMENT_SELECTION_V2_COMMITTED,
    ]
    assert events[1].sequence == 2
    assert events[1].previous_event_hash == events[0].event_id
    assert events[1].event_id == receipt.event_id

    # v1 flows are untouched: the 0C proposal still parses through event_proposal
    assert event_proposal(events[0]).command_kind == "remove_segment"
    # the v2 payload is not a 0C proposal — direct parsing is refused with a typed error
    with pytest.raises(EventStreamError):
        event_proposal(events[1])


def test_commit_refuses_validation_from_a_different_proposal(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        first = _proposal(_valid_candidates())
        foreign = validate_proposal(
            first, api, assemble_evidence_v2(api, first.candidates, source_id=SOURCE_ID)
        )
        second_candidates = (
            *_valid_candidates()[:2],
            _candidate("cand-g", "b_roll", 600, 610, intent="optional", refs=("shot-g",)),
        )
        second = _proposal(second_candidates, proposal_id="prop-moment-2")

        with pytest.raises(ValidationMismatchError):
            commit_selection(second, foreign, store=_store(tmp_path))


def test_commit_refuses_proposal_from_a_foreign_episode(tmp_path: Path) -> None:
    with open_api(make_episode_artifact(), tmp_path) as api:
        candidates = _valid_candidates()
        bundle = assemble_evidence_v2(api, candidates, source_id=SOURCE_ID)
        stranger = MomentSelectionProposalV2(
            proposal_id="prop-stranger",
            episode_id="ep-other",
            candidates=candidates,
        )
        result = validate_proposal(stranger, api, bundle)

        with pytest.raises(EpisodeMismatchError):
            commit_selection(stranger, result, store=_store(tmp_path))
