"""Todo 41 acceptance: atomic commit, versioning, conflict/no-partial publish,
and committed-only downstream lookup.

Proposals come from the Todo-40 builders over frozen Phase-1 fixture
manifests. Validators recompute (never trust proposal claims), the
state adoption is verified by CAS read-back, and every crash seam
between publish and state reconciles idempotently without a second
version for the same content and base.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.artifact_store.store import ArtifactStore
from services.foundation_io import canonical_model_bytes
from services.job_runner.cas import current_job_state
from services.validate.selection_lookup import (
    SelectionLookupError,
    get_committed_plan,
    get_committed_plan_by_artifact_id,
    load_committed_plan_file,
)
from services.validate.selection_models import (
    CommittedSelectionPlan,
    SelectionCommitOutcome,
    SelectionCommitRefusal,
    SelectionLock,
)
from services.validate.selection_plan_store import (
    initialize_plan_store,
    latest_version,
    load_events,
    load_index,
)
from services.validate.selection_schema import tuplize
from services.validate.selection_validators import frozen_phase1_capability_allowlist
from tests.validate.support import (
    ALL_FIXTURE_IDS,
    BASE,
    JOB,
    context_for,
    proposal_for,
    reduced_proposal_for,
    rig_for,
    store_object_count,
)

GATE_POLICY = Path("config/gates/phase-1-technical-v1.json")


def as_outcome(result: object) -> SelectionCommitOutcome:
    assert isinstance(result, SelectionCommitOutcome)
    return result


def as_refusal(result: object) -> SelectionCommitRefusal:
    assert isinstance(result, SelectionCommitRefusal)
    return result


# ---------------------------------------------------------------- happy paths


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_valid_proposal_commits_immutable_version_with_state_adoption(
    tmp_path: Path, fixture_id: str
) -> None:
    rig = rig_for(tmp_path, fixture_id)
    proposal = proposal_for(fixture_id)
    before = current_job_state(rig.state, JOB)
    assert before.status == "PLAN_PROPOSED"

    outcome = as_outcome(rig.commit(proposal))

    assert outcome.version == 1
    assert outcome.idempotent is False
    assert outcome.state_changed is True
    assert outcome.proposal_artifact_id == proposal.proposal_id
    index = load_index(rig.plan_dir)
    entry = index.versions["1"]
    assert entry.plan_sha256 == outcome.plan_sha256
    assert entry.parent_version is None
    assert entry.event_id == outcome.event_id
    payload, _ref = rig.artifacts.reopen(outcome.plan_sha256)
    document: object = json.loads(payload)
    plan = CommittedSelectionPlan.model_validate(tuplize(document))
    assert plan.plan_version == "v1"
    assert plan.episode_id == fixture_id
    assert plan.proposal == proposal
    assert canonical_model_bytes(plan) == payload
    registry_ids = set(rig.registry.load().entries)
    assert {proposal.proposal_id, outcome.plan_artifact_id} <= registry_ids
    after = current_job_state(rig.state, JOB)
    assert after.status == "PLAN_COMMITTED"
    assert after.adopted_artifact_hash == outcome.plan_sha256
    assert outcome.adopted == after
    assert store_object_count(rig.artifacts.store_root) == 2


def test_commit_twice_is_idempotent(tmp_path: Path) -> None:
    rig = rig_for(tmp_path)
    proposal = proposal_for("p1-ref-02-pauses-fillers")

    first = as_outcome(rig.commit(proposal))
    second = as_outcome(rig.commit(proposal))

    assert second.idempotent is True
    assert second.version == first.version == 1
    assert second.plan_sha256 == first.plan_sha256
    assert second.event_id == first.event_id
    assert second.state_changed is False
    assert second.adopted.updated_at_seq == first.adopted.updated_at_seq
    assert latest_version(load_index(rig.plan_dir)) == 1
    assert store_object_count(rig.artifacts.store_root) == 2
    committed = [e for e in load_events(rig.plan_dir) if e.kind == "plan_committed"]
    assert len(committed) == 1


def test_distinct_proposals_advance_versions(tmp_path: Path) -> None:
    rig = rig_for(tmp_path)

    first = as_outcome(rig.commit(proposal_for("p1-ref-02-pauses-fillers")))
    second = as_outcome(
        rig.commit(proposal_for("p1-ref-02-pauses-fillers", base="v1"))
    )

    assert (first.version, second.version) == (1, 2)
    assert second.plan_sha256 != first.plan_sha256
    index = load_index(rig.plan_dir)
    assert index.versions["2"].parent_version == 1
    events = [e for e in load_events(rig.plan_dir) if e.kind == "plan_committed"]
    assert [e.result_version for e in events] == ["v1", "v2"]
    assert events[1].previous_event_hash == events[0].event_id
    after = current_job_state(rig.state, JOB)
    assert after.adopted_artifact_hash == second.plan_sha256
    assert store_object_count(rig.artifacts.store_root) == 4

    replayed_old = as_outcome(rig.commit(proposal_for("p1-ref-02-pauses-fillers")))
    assert replayed_old.idempotent is True
    assert replayed_old.version == 1
    assert replayed_old.state_changed is False
    assert current_job_state(rig.state, JOB).adopted_artifact_hash == second.plan_sha256


# --------------------------------------------------------------- refusals


def test_schema_float_refusal_records_event_without_publish(tmp_path: Path) -> None:
    rig = rig_for(tmp_path)
    before = current_job_state(rig.state, JOB)
    document: dict[str, object] = dict(
        proposal_for("p1-ref-02-pauses-fillers").model_dump(mode="json")  # type: ignore[arg-type]
    )
    candidates = document["candidates"]
    assert isinstance(candidates, list)
    first = candidates[0]
    assert isinstance(first, dict)
    first["duration_frames"] = 150.5

    refusal = as_refusal(rig.commit(document))

    assert refusal.validator == "schema"
    assert refusal.code == "schema_invalid"
    assert refusal.event_id is not None
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before
    events = load_events(rig.plan_dir)
    assert [e.refusal_code for e in events] == ["schema_invalid"]
    replayed = as_refusal(rig.commit(document))
    assert replayed.event_id == refusal.event_id


def test_malformed_proposal_json_is_refused(tmp_path: Path) -> None:
    rig = rig_for(tmp_path)
    refusal = as_refusal(rig.commit("{not a proposal"))
    assert refusal.validator == "schema"
    assert refusal.code == "schema_invalid"
    assert store_object_count(rig.artifacts.store_root) == 0


def test_semantic_identity_mismatch_is_refused(tmp_path: Path) -> None:
    rig = rig_for(tmp_path)
    before = current_job_state(rig.state, JOB)
    proposal = proposal_for("p1-ref-02-pauses-fillers")
    tampered_candidate = proposal.candidates[0].model_copy(update={"candidate_id": "0" * 64})
    tampered = proposal.model_copy(
        update={"candidates": (tampered_candidate, *proposal.candidates[1:])}
    )

    refusal = as_refusal(rig.commit(tampered))

    assert refusal.validator == "semantic"
    assert refusal.code == "identity_mismatch"
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before
    assert [e.refusal_validator for e in load_events(rig.plan_dir)] == ["semantic"]


def test_lock_conflict_is_deferred_and_recorded(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    proposal = proposal_for(fixture_id)
    removed = next(c for c in proposal.candidates if c.intent == "remove")
    lock = SelectionLock(
        field="selection",
        source_id=removed.source_ref.source_id,
        edit_source_sha=removed.source_ref.edit_source_sha,
        span=removed.span,
        base_plan_version=BASE,
        grantor="operator-review",
        basis="manual review kept this breathing room",
        granted_at_seq=7,
    )
    rig = rig_for(tmp_path, fixture_id, context=context_for(fixture_id, locks=(lock,)))
    before = current_job_state(rig.state, JOB)

    refusal = as_refusal(rig.commit(proposal))

    assert refusal.validator == "lock"
    assert refusal.code == "lock_conflict"
    assert refusal.deferred is True
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before
    events = load_events(rig.plan_dir)
    assert [(e.refusal_validator, e.refusal_code) for e in events] == [
        ("lock", "lock_conflict")
    ]


def test_capability_missing_is_refused(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    frozen = frozen_phase1_capability_allowlist(GATE_POLICY)
    assert "base_cut" in frozen
    assert "fixed_subtitle" in frozen
    rig = rig_for(
        tmp_path, fixture_id, context=context_for(fixture_id, allowlist=("fixed_subtitle",))
    )
    before = current_job_state(rig.state, JOB)

    refusal = as_refusal(rig.commit(proposal_for(fixture_id)))

    assert refusal.validator == "capability"
    assert refusal.code == "capability_missing"
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before


def test_episode_contract_mismatch_is_refused(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    episode = context_for(fixture_id).episode
    variants = (
        episode.model_copy(update={"fixture_only": False}),
        episode.model_copy(update={"status": "assisted"}),
    )
    for index, variant in enumerate(variants):
        rig = rig_for(
            tmp_path / f"variant-{index}",
            fixture_id,
            context=context_for(fixture_id, episode=variant),
        )
        refusal = as_refusal(rig.commit(proposal_for(fixture_id)))
        assert refusal.validator == "episode_contract"
        assert refusal.code == "episode_contract_mismatch"
        assert store_object_count(rig.artifacts.store_root) == 0


def test_absent_evidence_is_refused_at_schema(tmp_path: Path) -> None:
    rig = rig_for(tmp_path)
    document: dict[str, object] = dict(
        proposal_for("p1-ref-02-pauses-fillers").model_dump(mode="json")  # type: ignore[arg-type]
    )
    candidates = document["candidates"]
    assert isinstance(candidates, list)
    first = candidates[0]
    assert isinstance(first, dict)
    first["evidence"] = []

    refusal = as_refusal(rig.commit(document))

    assert refusal.validator == "schema"
    assert refusal.code == "schema_invalid"
    assert "evidence" in refusal.detail
    assert store_object_count(rig.artifacts.store_root) == 0


def test_stale_base_conflict_publishes_nothing_partial(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    rig = rig_for(tmp_path)
    first = as_outcome(rig.commit(proposal_for(fixture_id)))
    objects_after_first = store_object_count(rig.artifacts.store_root)
    stale = reduced_proposal_for(fixture_id, base=BASE)

    refusal = as_refusal(rig.commit(stale))

    assert refusal.validator is None
    assert refusal.code == "stale_base"
    assert store_object_count(rig.artifacts.store_root) == objects_after_first
    assert latest_version(load_index(rig.plan_dir)) == 1
    after = current_job_state(rig.state, JOB)
    assert after.adopted_artifact_hash == first.plan_sha256
    replayed = as_refusal(rig.commit(stale))
    assert replayed.event_id == refusal.event_id
    assert store_object_count(rig.artifacts.store_root) == objects_after_first


# ------------------------------------------------------- crash reconciliation


def test_crash_between_publish_and_state_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = rig_for(tmp_path)
    proposal = proposal_for("p1-ref-02-pauses-fillers")
    before = current_job_state(rig.state, JOB)

    def crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated crash after publish, before state CAS")

    monkeypatch.setattr("services.job_runner.lanes.apply_transition", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        rig.commit(proposal)
    monkeypatch.undo()

    index = load_index(rig.plan_dir)
    assert "1" in index.versions
    assert current_job_state(rig.state, JOB) == before

    recovered = as_outcome(rig.commit(proposal))

    assert recovered.idempotent is True
    assert recovered.version == 1
    assert recovered.plan_sha256 == index.versions["1"].plan_sha256
    assert recovered.state_changed is True
    after = current_job_state(rig.state, JOB)
    assert after.status == "PLAN_COMMITTED"
    assert after.adopted_artifact_hash == recovered.plan_sha256
    assert latest_version(load_index(rig.plan_dir)) == 1
    assert store_object_count(rig.artifacts.store_root) == 2


def test_crash_between_event_and_publish_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = rig_for(tmp_path)
    proposal = proposal_for("p1-ref-02-pauses-fillers")
    before = current_job_state(rig.state, JOB)

    def crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated crash after the commit event, before publish")

    monkeypatch.setattr(ArtifactStore, "publish", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        rig.commit(proposal)
    monkeypatch.undo()

    events = [e for e in load_events(rig.plan_dir) if e.kind == "plan_committed"]
    assert [e.result_version for e in events] == ["v1"]
    assert latest_version(load_index(rig.plan_dir)) == 0
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before

    recovered = as_outcome(rig.commit(proposal))

    assert recovered.version == 1
    assert recovered.event_id == events[0].event_id
    assert latest_version(load_index(rig.plan_dir)) == 1
    committed = [e for e in load_events(rig.plan_dir) if e.kind == "plan_committed"]
    assert len(committed) == 1
    after = current_job_state(rig.state, JOB)
    assert after.adopted_artifact_hash == recovered.plan_sha256


# ---------------------------------------------------------- committed-only


def test_committed_only_lookup(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    rig = rig_for(tmp_path)
    proposal = proposal_for(fixture_id)
    outcome = as_outcome(rig.commit(proposal))

    latest = get_committed_plan(rig.artifacts, rig.plan_dir)
    explicit = get_committed_plan(rig.artifacts, rig.plan_dir, version=1)
    payload, _ref = rig.artifacts.reopen(outcome.plan_sha256)
    assert latest.payload == explicit.payload == payload
    assert latest.plan_version == explicit.plan_version == 1
    assert latest.content_sha256 == outcome.plan_sha256
    assert latest.plan_artifact_id == outcome.plan_artifact_id

    served = get_committed_plan_by_artifact_id(rig.artifacts, outcome.plan_artifact_id)
    assert served.payload == payload
    assert served.plan_version == 1

    with pytest.raises(SelectionLookupError) as proposal_id_error:
        get_committed_plan_by_artifact_id(rig.artifacts, proposal.proposal_id)
    assert proposal_id_error.value.code == "committed_only"

    with pytest.raises(SelectionLookupError) as unknown_version_error:
        get_committed_plan(rig.artifacts, rig.plan_dir, version=99)
    assert unknown_version_error.value.code == "unknown_version"

    proposal_file = tmp_path / "selection-proposal.json"
    proposal_file.write_bytes(canonical_model_bytes(proposal))
    with pytest.raises(SelectionLookupError) as file_error:
        load_committed_plan_file(proposal_file)
    assert file_error.value.code == "committed_only"

    committed_file = tmp_path / "plan-v1.json"
    committed_file.write_bytes(payload)
    plan = load_committed_plan_file(committed_file)
    assert isinstance(plan, CommittedSelectionPlan)
    assert plan.plan_version == "v1"

    empty_dir = tmp_path / "empty-plan-store"
    initialize_plan_store(empty_dir, episode_id=fixture_id, base_version=BASE)
    with pytest.raises(SelectionLookupError) as empty_error:
        get_committed_plan(rig.artifacts, empty_dir)
    assert empty_error.value.code == "no_committed_version"
