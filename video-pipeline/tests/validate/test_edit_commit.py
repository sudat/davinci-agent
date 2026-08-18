"""Todo 43 acceptance (commit side): the serialized Edit Commit authority.

Validators recompute everything (never trusting proposal claims), the stale
SELECTION base is a conflict before any publish, lock conflicts defer, Resolve
fields/LLM uuids/floats are refused at schema, and every crash seam between
publish and state reconciles idempotently without a second version for the
same content and base. Downstream consumers read committed versions only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.editorial.candidate_models import ProposalProducer
from services.foundation_io import canonical_model_bytes
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.plan.edit_plan_models import RecordSpan, SelectionPlanRef
from services.validate.edit_commit import EditCommitAuthority
from services.validate.edit_commit_lookup import (
    EditLookupError,
    get_committed_edit_plan,
    get_committed_edit_plan_by_artifact_id,
    load_committed_edit_plan_file,
)
from services.validate.edit_commit_models import (
    CommittedEditPlan,
    EditCommitOutcome,
    EditCommitRefusal,
    EditValidationContext,
)
from services.validate.edit_commit_schema import tuplize
from services.validate.edit_commit_store import (
    initialize_edit_plan_store,
    latest_edit_version,
    load_edit_index,
    load_events,
)
from services.validate.selection_models import (
    EditSourceFacts,
    SelectionLock,
    episode_record_from_manifest,
)
from tests.editorial.support import load_manifest
from tests.editorial.test_selection_proposal import pool_for
from tests.plan.support import ALL_FIXTURE_IDS
from tests.plan.test_edit_plan import (
    EDIT_BASE,
    _selection_for,
    generated_for,
    selection_ref_for,
)
from tests.validate.support import store_object_count

JOB = "job-edit-1"


def as_outcome(result: object) -> EditCommitOutcome:
    assert isinstance(result, EditCommitOutcome)
    return result


def as_refusal(result: object) -> EditCommitRefusal:
    assert isinstance(result, EditCommitRefusal)
    return result


def _context_for(
    fixture_id: str,
    *,
    selection_base: SelectionPlanRef | None = None,
    locks: tuple[SelectionLock, ...] = (),
    allowlist: tuple[str, ...] = PHASE_0A_CAPABILITIES,
) -> EditValidationContext:
    manifest = load_manifest(fixture_id)
    pool = pool_for(manifest)
    return EditValidationContext(
        episode=episode_record_from_manifest(manifest),
        edit_source=EditSourceFacts(
            source_id=pool.source_id,
            edit_source_sha=pool.edit_source_sha,
            total_frames=pool.total_frames,
        ),
        capability_allowlist=tuple(allowlist),
        selection_base=selection_base
        if selection_base is not None
        else selection_ref_for(fixture_id, _selection_for(fixture_id)),
        locks=locks,
    )


@dataclass
class EditRig:
    state: StateStore
    artifacts: ArtifactStore
    registry: ArtifactRegistry
    authority: EditCommitAuthority
    edit_dir: Path

    def commit(self, document: object, *, now: int = 100, ttl_seconds: int = 100):
        return self.authority.commit(document, now=now, ttl_seconds=ttl_seconds)


def _advance_to_plan_proposed(state: StateStore) -> None:
    snapshot = current_job_state(state, JOB)
    for target in ("INGESTED", "NORMALIZED", "ANALYZED", "PLAN_PROPOSED"):
        snapshot = apply_transition(
            state,
            JOB,
            expected_status=snapshot.status,
            expected_parent_hash=snapshot.adopted_artifact_hash,
            new_status=target,
            new_artifact_hash=hashlib.sha256(f"step-{target}".encode()).hexdigest(),
        )


def _rig_for(
    tmp_path: Path,
    fixture_id: str = "p1-ref-02-pauses-fillers",
    *,
    context: EditValidationContext | None = None,
) -> EditRig:
    manifest = load_manifest(fixture_id)
    state = StateStore.open(tmp_path / "state.sqlite3")
    state.create_job(job_id=JOB, episode_id=manifest.fixture_id, current_stage="editorial")
    _advance_to_plan_proposed(state)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    registry = ArtifactRegistry(tmp_path / "registry")
    edit_dir = tmp_path / "edit-plan-store"
    initialize_edit_plan_store(edit_dir, episode_id=manifest.fixture_id, base_version=EDIT_BASE)
    authority = EditCommitAuthority(
        artifact_store=artifacts,
        registry=registry,
        state_store=state,
        job_id=JOB,
        edit_plan_dir=edit_dir,
        context=context if context is not None else _context_for(fixture_id),
        selection_plan=_selection_for(fixture_id),
        holder="edit-commit-a",
    )
    return EditRig(
        state=state,
        artifacts=artifacts,
        registry=registry,
        authority=authority,
        edit_dir=edit_dir,
    )


# ---------------------------------------------------------------- happy paths


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_valid_edit_plan_commits_immutable_version_with_state_adoption(
    tmp_path: Path, fixture_id: str
) -> None:
    rig = _rig_for(tmp_path, fixture_id)
    plan = generated_for(fixture_id)
    before = current_job_state(rig.state, JOB)
    assert before.status == "PLAN_PROPOSED"

    outcome = as_outcome(rig.commit(plan))

    assert outcome.version == 1
    assert outcome.idempotent is False
    assert outcome.state_changed is True
    assert outcome.proposal_artifact_id == plan.proposal_id
    index = load_edit_index(rig.edit_dir)
    entry = index.versions["1"]
    assert entry.plan_sha256 == outcome.plan_sha256
    assert entry.parent_version is None
    assert entry.event_id == outcome.event_id
    payload, _ref = rig.artifacts.reopen(outcome.plan_sha256)
    document: object = json.loads(payload)
    committed = CommittedEditPlan.model_validate(tuplize(document))
    assert committed.plan_version == "v1"
    assert committed.episode_id == fixture_id
    assert committed.proposal == plan
    assert canonical_model_bytes(committed) == payload
    registry_ids = set(rig.registry.load().entries)
    assert {plan.proposal_id, outcome.plan_artifact_id} <= registry_ids
    after = current_job_state(rig.state, JOB)
    assert after.status == "PLAN_COMMITTED"
    assert after.adopted_artifact_hash == outcome.plan_sha256
    assert outcome.adopted == after
    assert store_object_count(rig.artifacts.store_root) == 2


def test_commit_twice_is_idempotent(tmp_path: Path) -> None:
    rig = _rig_for(tmp_path)
    plan = generated_for("p1-ref-02-pauses-fillers")

    first = as_outcome(rig.commit(plan))
    second = as_outcome(rig.commit(plan))

    assert second.idempotent is True
    assert second.version == first.version == 1
    assert second.plan_sha256 == first.plan_sha256
    assert second.event_id == first.event_id
    assert second.state_changed is False
    assert second.adopted.updated_at_seq == first.adopted.updated_at_seq
    assert latest_edit_version(load_edit_index(rig.edit_dir)) == 1
    assert store_object_count(rig.artifacts.store_root) == 2
    committed = [e for e in load_events(rig.edit_dir) if e.kind == "plan_committed"]
    assert len(committed) == 1


def test_distinct_edit_plans_advance_versions(tmp_path: Path) -> None:
    rig = _rig_for(tmp_path)
    fixture_id = "p1-ref-02-pauses-fillers"

    first = as_outcome(rig.commit(generated_for(fixture_id)))
    second = as_outcome(rig.commit(generated_for(fixture_id, plan_base_version="v1")))

    assert (first.version, second.version) == (1, 2)
    assert second.plan_sha256 != first.plan_sha256
    index = load_edit_index(rig.edit_dir)
    assert index.versions["2"].parent_version == 1
    events = [e for e in load_events(rig.edit_dir) if e.kind == "plan_committed"]
    assert [e.result_version for e in events] == ["v1", "v2"]
    assert events[1].previous_event_hash == events[0].event_id
    after = current_job_state(rig.state, JOB)
    assert after.adopted_artifact_hash == second.plan_sha256
    assert store_object_count(rig.artifacts.store_root) == 4

    replayed_old = as_outcome(rig.commit(generated_for(fixture_id)))
    assert replayed_old.idempotent is True
    assert replayed_old.version == 1
    assert replayed_old.state_changed is False
    assert current_job_state(rig.state, JOB).adopted_artifact_hash == second.plan_sha256


# ------------------------------------------------------------------ refusals


def test_stale_selection_base_conflict_publishes_nothing_partial(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    stale_pin = SelectionPlanRef(
        episode_id=fixture_id,
        plan_version="v2",
        plan_sha256=hashlib.sha256(b"a-newer-selection").hexdigest(),
        plan_artifact_id=f"selection-plan.{fixture_id}.v2",
    )
    rig = _rig_for(tmp_path, fixture_id, context=_context_for(fixture_id, selection_base=stale_pin))
    before = current_job_state(rig.state, JOB)
    plan = generated_for(fixture_id)

    refusal = as_refusal(rig.commit(plan))

    assert refusal.validator is None
    assert refusal.code == "stale_selection"
    assert store_object_count(rig.artifacts.store_root) == 0
    assert latest_edit_version(load_edit_index(rig.edit_dir)) == 0
    assert current_job_state(rig.state, JOB) == before
    replayed = as_refusal(rig.commit(plan))
    assert replayed.event_id == refusal.event_id
    assert store_object_count(rig.artifacts.store_root) == 0


def test_stale_own_chain_base_is_refused(tmp_path: Path) -> None:
    rig = _rig_for(tmp_path)
    fixture_id = "p1-ref-02-pauses-fillers"
    as_outcome(rig.commit(generated_for(fixture_id)))
    objects_after_first = store_object_count(rig.artifacts.store_root)
    distinct_stale = generated_for(fixture_id).model_copy(
        update={
            "producer": ProposalProducer(
                model_role_id="constraint-planner", contract_version="todo43-v2"
            )
        }
    )

    refusal = as_refusal(rig.commit(distinct_stale))

    assert refusal.validator is None
    assert refusal.code == "stale_base"
    assert latest_edit_version(load_edit_index(rig.edit_dir)) == 1
    assert store_object_count(rig.artifacts.store_root) == objects_after_first


def test_broken_link_is_refused_without_publish(tmp_path: Path) -> None:
    rig = _rig_for(tmp_path)
    before = current_job_state(rig.state, JOB)
    plan = generated_for("p1-ref-02-pauses-fillers")
    audio = next(item for item in plan.items if item.track_kind == "audio")
    broken = audio.model_copy(update={"link_group_id": "0" * 64})
    tampered = plan.model_copy(
        update={"items": tuple(broken if i is audio else i for i in plan.items)}
    )

    refusal = as_refusal(rig.commit(tampered))

    assert refusal.validator == "semantic"
    assert refusal.code == "broken_link"
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before
    assert [e.refusal_code for e in load_events(rig.edit_dir)] == ["broken_link"]


def test_invalid_record_span_is_refused_without_publish(tmp_path: Path) -> None:
    rig = _rig_for(tmp_path)
    before = current_job_state(rig.state, JOB)
    plan = generated_for("p1-ref-02-pauses-fillers")
    video = next(item for item in plan.items if item.track_kind == "video")
    audio = next(
        item for item in plan.items if item.track_kind == "audio"
        and item.link_group_id == video.item_id
    )
    shifted = RecordSpan(start_frame=999, end_frame=999 + video.record_span.length)
    tampered_items = tuple(
        item.model_copy(update={"record_span": shifted})
        if item in (video, audio)
        else item
        for item in plan.items
    )
    tampered = plan.model_copy(update={"items": tampered_items})

    refusal = as_refusal(rig.commit(tampered))

    assert refusal.validator == "semantic"
    assert refusal.code == "record_not_contiguous"
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before


def test_lock_conflict_is_deferred_and_recorded(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    selection = _selection_for(fixture_id)
    removed = next(c for c in selection.candidates if c.intent == "remove")
    lock = SelectionLock(
        field="selection",
        source_id=removed.source_ref.source_id,
        edit_source_sha=removed.source_ref.edit_source_sha,
        span=removed.span,
        base_plan_version="plan-base-v0",
        grantor="operator-review",
        basis="manual review kept this breathing room",
        granted_at_seq=7,
    )
    rig = _rig_for(
        tmp_path, fixture_id, context=_context_for(fixture_id, locks=(lock,))
    )
    before = current_job_state(rig.state, JOB)

    refusal = as_refusal(rig.commit(generated_for(fixture_id)))

    assert refusal.validator == "lock"
    assert refusal.code == "lock_conflict"
    assert refusal.deferred is True
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before
    assert [(e.refusal_validator, e.refusal_code) for e in load_events(rig.edit_dir)] == [
        ("lock", "lock_conflict")
    ]


def test_resolve_and_uuid_field_injection_is_refused_without_publish(tmp_path: Path) -> None:
    rig = _rig_for(tmp_path)
    before = current_job_state(rig.state, JOB)
    plan = generated_for("p1-ref-02-pauses-fillers")

    resolve_document: dict[str, object] = dict(plan.model_dump(mode="json"))
    resolve_document["resolve_track_index"] = 1
    resolve_refusal = as_refusal(rig.commit(resolve_document))
    assert resolve_refusal.validator == "schema"
    assert resolve_refusal.code == "schema_invalid"
    assert "resolve_field_forbidden" in resolve_refusal.detail

    track_document: dict[str, object] = dict(plan.model_dump(mode="json"))
    track_document["track_index"] = 1
    track_refusal = as_refusal(rig.commit(track_document))
    assert track_refusal.validator == "schema"
    assert track_refusal.code == "schema_invalid"

    uuid_document: dict[str, object] = dict(plan.model_dump(mode="json"))
    uuid_document["decision_uuid"] = "llm-authored"
    uuid_refusal = as_refusal(rig.commit(uuid_document))
    assert uuid_refusal.validator == "schema"
    assert uuid_refusal.code == "schema_invalid"
    assert "llm_uuid_forbidden" in uuid_refusal.detail

    float_document: dict[str, object] = dict(plan.model_dump(mode="json"))
    float_document["total_duration_frames"] = 614.5
    float_refusal = as_refusal(rig.commit(float_document))
    assert float_refusal.validator == "schema"
    assert float_refusal.code == "schema_invalid"
    assert "float" in float_refusal.detail

    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before


def test_capability_missing_is_refused(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    rig = _rig_for(
        tmp_path, fixture_id, context=_context_for(fixture_id, allowlist=("fixed_subtitle",))
    )
    before = current_job_state(rig.state, JOB)

    refusal = as_refusal(rig.commit(generated_for(fixture_id)))

    assert refusal.validator == "capability"
    assert refusal.code == "capability_missing"
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before


# ------------------------------------------------------- crash reconciliation


def test_crash_cannot_partially_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig_for(tmp_path)
    plan = generated_for("p1-ref-02-pauses-fillers")
    before = current_job_state(rig.state, JOB)

    def crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated crash after publish, before state CAS")

    monkeypatch.setattr("services.job_runner.lanes.apply_transition", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        rig.commit(plan)
    monkeypatch.undo()

    index = load_edit_index(rig.edit_dir)
    assert "1" in index.versions
    assert current_job_state(rig.state, JOB) == before, "no partial adoption"
    assert get_committed_edit_plan(rig.artifacts, rig.edit_dir).plan_version == 1

    recovered = as_outcome(rig.commit(plan))

    assert recovered.idempotent is True
    assert recovered.version == 1
    assert recovered.plan_sha256 == index.versions["1"].plan_sha256
    assert recovered.state_changed is True
    after = current_job_state(rig.state, JOB)
    assert after.status == "PLAN_COMMITTED"
    assert after.adopted_artifact_hash == recovered.plan_sha256
    assert latest_edit_version(load_edit_index(rig.edit_dir)) == 1
    assert store_object_count(rig.artifacts.store_root) == 2


def test_crash_between_event_and_publish_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig_for(tmp_path)
    plan = generated_for("p1-ref-02-pauses-fillers")
    before = current_job_state(rig.state, JOB)

    def crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated crash after the commit event, before publish")

    monkeypatch.setattr(ArtifactStore, "publish", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        rig.commit(plan)
    monkeypatch.undo()

    events = [e for e in load_events(rig.edit_dir) if e.kind == "plan_committed"]
    assert [e.result_version for e in events] == ["v1"]
    assert latest_edit_version(load_edit_index(rig.edit_dir)) == 0
    assert store_object_count(rig.artifacts.store_root) == 0
    assert current_job_state(rig.state, JOB) == before

    recovered = as_outcome(rig.commit(plan))

    assert recovered.version == 1
    assert recovered.event_id == events[0].event_id
    assert latest_edit_version(load_edit_index(rig.edit_dir)) == 1
    committed = [e for e in load_events(rig.edit_dir) if e.kind == "plan_committed"]
    assert len(committed) == 1
    after = current_job_state(rig.state, JOB)
    assert after.adopted_artifact_hash == recovered.plan_sha256


# ---------------------------------------------------------- committed-only


def test_committed_only_lookup(tmp_path: Path) -> None:
    fixture_id = "p1-ref-02-pauses-fillers"
    rig = _rig_for(tmp_path)
    plan = generated_for(fixture_id)
    outcome = as_outcome(rig.commit(plan))

    latest = get_committed_edit_plan(rig.artifacts, rig.edit_dir)
    explicit = get_committed_edit_plan(rig.artifacts, rig.edit_dir, version=1)
    payload, _ref = rig.artifacts.reopen(outcome.plan_sha256)
    assert latest.payload == explicit.payload == payload
    assert latest.plan_version == explicit.plan_version == 1
    assert latest.content_sha256 == outcome.plan_sha256
    assert latest.plan_artifact_id == outcome.plan_artifact_id

    served = get_committed_edit_plan_by_artifact_id(rig.artifacts, outcome.plan_artifact_id)
    assert served.payload == payload
    assert served.plan_version == 1

    with pytest.raises(EditLookupError) as proposal_id_error:
        get_committed_edit_plan_by_artifact_id(rig.artifacts, plan.proposal_id)
    assert proposal_id_error.value.code == "committed_only"

    with pytest.raises(EditLookupError) as unknown_version_error:
        get_committed_edit_plan(rig.artifacts, rig.edit_dir, version=99)
    assert unknown_version_error.value.code == "unknown_version"

    proposal_file = tmp_path / "edit-proposal.json"
    proposal_file.write_bytes(canonical_model_bytes(plan))
    with pytest.raises(EditLookupError) as file_error:
        load_committed_edit_plan_file(proposal_file)
    assert file_error.value.code == "committed_only"

    committed_file = tmp_path / "edit-plan-v1.json"
    committed_file.write_bytes(payload)
    committed_model = load_committed_edit_plan_file(committed_file)
    assert isinstance(committed_model, CommittedEditPlan)
    assert committed_model.plan_version == "v1"

    empty_dir = tmp_path / "empty-edit-store"
    initialize_edit_plan_store(empty_dir, episode_id=fixture_id, base_version=EDIT_BASE)
    with pytest.raises(EditLookupError) as empty_error:
        get_committed_edit_plan(rig.artifacts, empty_dir)
    assert empty_error.value.code == "no_committed_version"
