"""Job State Machine transition-table and CAS tests (Todo 10).

The full legal walk chains REAL published artifact-store hashes as
parents so the runtime rows stay consistent with ``verify_against_store``;
forbidden/auxiliary cases drive the machine with synthetic valid sha256
values because refused transitions never write.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from services.job_runner.cas import (
    ApprovalRef,
    CasError,
    TransitionPayload,
    apply_transition,
    current_job_state,
)
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_integrity import verify_against_store
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import (
    APPROVAL_PURPOSE_EDITORIAL,
    APPROVAL_PURPOSE_FINAL,
    MAIN_PATH,
    edge_for,
    is_gate_bypass,
)
from tests.job_runner.support import make_artifact_store, make_registry, publish_and_register

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.job_runner.cas import CasSnapshot
    from services.job_runner.state_models import JobStatus


def opened(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.sqlite3")
    store.create_job(job_id="job-1", episode_id="ep-1", current_stage="ingest")
    return store


def synthetic_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def approval_payload(parent: str, purpose: str, artifact_ref: str) -> TransitionPayload:
    return TransitionPayload(
        approval=ApprovalRef(
            purpose=purpose,
            target_hash=parent,
            artifact_ref=artifact_ref,
        )
    )


GATED_PURPOSES = {"EDITORIAL_APPROVED": APPROVAL_PURPOSE_EDITORIAL,
                  "FINAL_APPROVED": APPROVAL_PURPOSE_FINAL}


def advance(
    store: StateStore, *, to_status: str, hash_for: dict[str, str]
) -> CasSnapshot:
    """Walk the main path forward from the current status to ``to_status``."""

    snapshot = current_job_state(store, "job-1")
    target_index = MAIN_PATH.index(cast("JobStatus", to_status))
    if MAIN_PATH.index(snapshot.status) == target_index:
        return snapshot
    assert MAIN_PATH.index(snapshot.status) < target_index, "advance only moves forward"
    for target in MAIN_PATH[MAIN_PATH.index(snapshot.status) + 1 : target_index + 1]:
        payload = (
            approval_payload(
                snapshot.adopted_artifact_hash or "",
                GATED_PURPOSES[str(target)],
                f"setup-ref-{GATED_PURPOSES[str(target)]}",
            )
            if str(target) in GATED_PURPOSES
            else None
        )
        snapshot = apply_transition(
            store,
            "job-1",
            expected_status=snapshot.status,
            expected_parent_hash=snapshot.adopted_artifact_hash,
            new_status=target,
            new_artifact_hash=hash_for[str(target)],
            payload=payload,
        )
    return snapshot


def drive_to(store: StateStore, status: str) -> CasSnapshot:
    """Advance to ``status`` using synthetic hashes (setup for refusal cases)."""

    hashes = {str(step): synthetic_hash(f"setup-{step}") for step in MAIN_PATH}
    return advance(store, to_status=status, hash_for=hashes)


def test_legal_full_path_walk(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        artifact_store: ArtifactStore = make_artifact_store(tmp_path)
        registry: ArtifactRegistry = make_registry(tmp_path)
        real_hashes: dict[str, str] = {}
        for status in MAIN_PATH[1:]:
            artifact_id = f"artifact-{str(status).lower()}"
            receipt = publish_and_register(
                artifact_store, registry, artifact_id, f"payload-{status}".encode()
            )
            real_hashes[str(status)] = receipt.content_sha256

        snapshot = current_job_state(store, "job-1")
        assert (snapshot.status, snapshot.adopted_artifact_hash) == ("CREATED", None)

        previous_seq = snapshot.updated_at_seq
        for index, status in enumerate(MAIN_PATH[1:], start=1):
            payload = (
                approval_payload(
                    snapshot.adopted_artifact_hash or "",
                    GATED_PURPOSES[str(status)],
                    f"artifact-{str(MAIN_PATH[index - 1]).lower()}",
                )
                if str(status) in GATED_PURPOSES
                else None
            )
            snapshot = apply_transition(
                store,
                "job-1",
                expected_status=snapshot.status,
                expected_parent_hash=snapshot.adopted_artifact_hash,
                new_status=status,
                new_artifact_hash=real_hashes[str(status)],
                payload=payload,
            )
            assert snapshot.status == status
            assert snapshot.adopted_artifact_hash == real_hashes[str(status)]
            assert snapshot.updated_at_seq > previous_seq
            previous_seq = snapshot.updated_at_seq

        final = current_job_state(store, "job-1")
        assert final.status == "FROZEN"
        assert final.adopted_artifact_hash == real_hashes["FROZEN"]
        approvals = store.get_approval_refs()
        assert {ref.purpose for ref in approvals} == {
            APPROVAL_PURPOSE_EDITORIAL,
            APPROVAL_PURPOSE_FINAL,
        }
        assert {ref.target_hash for ref in approvals} == {
            real_hashes["PREVIEW_READY"],
            real_hashes["QC_PASSED"],
        }
        report = verify_against_store(store, artifact_store, registry)
        assert report.checked_approval_refs == 2


@pytest.mark.parametrize(
    ("current", "target", "code"),
    [
        ("CREATED", "ANALYZED", "forbidden-transition"),
        ("PLAN_COMMITTED", "RESOLVE_BUILT", "forbidden-transition"),
        ("PREVIEW_READY", "RESOLVE_BUILT", "forbidden-approval-required"),
        ("INGESTED", "CREATED", "forbidden-transition"),
        ("EDITORIAL_APPROVED", "PLAN_COMMITTED", "forbidden-transition"),
        ("FROZEN", "FROZEN", "forbidden-transition"),
        ("FROZEN", "CREATED", "forbidden-transition"),
    ],
    ids=[
        "created-skip-to-analyzed",
        "plan-committed-skip-to-resolve-built",
        "preview-ready-to-resolve-built-without-approval",
        "ingested-backward-to-created",
        "editorial-approved-backward-to-plan-committed",
        "frozen-self-forbidden",
        "frozen-backward-to-created",
    ],
)
def test_forbidden_edges_refused(current: str, target: str, code: str, tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        drive_to(store, current)
        before = current_job_state(store, "job-1")

        with pytest.raises(CasError, match=code) as error:
            apply_transition(
                store,
                "job-1",
                expected_status=before.status,
                expected_parent_hash=before.adopted_artifact_hash,
                new_status=cast("JobStatus", target),
                new_artifact_hash=synthetic_hash(f"forbidden-{target}"),
            )
        assert error.value.code == code
        assert current_job_state(store, "job-1") == before


def test_preview_ready_to_resolve_built_without_approval_refused(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        drive_to(store, "PREVIEW_READY")
        before = current_job_state(store, "job-1")
        assert before.status == "PREVIEW_READY"

        with pytest.raises(CasError, match="forbidden-approval-required") as error:
            apply_transition(
                store,
                "job-1",
                expected_status="PREVIEW_READY",
                expected_parent_hash=before.adopted_artifact_hash,
                new_status="RESOLVE_BUILT",
                new_artifact_hash=synthetic_hash("rogue-build"),
            )
        assert error.value.code == "forbidden-approval-required"
        assert is_gate_bypass("PREVIEW_READY", "RESOLVE_BUILT")
        assert current_job_state(store, "job-1") == before


def test_editorial_approval_requires_operator_checkpoint_ref(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        drive_to(store, "PREVIEW_READY")
        parent = current_job_state(store, "job-1").adopted_artifact_hash
        assert parent is not None

        with pytest.raises(CasError, match="forbidden-approval-required") as missing:
            apply_transition(
                store,
                "job-1",
                expected_status="PREVIEW_READY",
                expected_parent_hash=parent,
                new_status="EDITORIAL_APPROVED",
                new_artifact_hash=synthetic_hash("approved-plan"),
                payload=TransitionPayload(approval=None),
            )
        assert missing.value.code == "forbidden-approval-required"

        wrong_target = TransitionPayload(
            approval=ApprovalRef(
                purpose=APPROVAL_PURPOSE_EDITORIAL,
                target_hash=synthetic_hash("not-the-preview"),
                artifact_ref="approval-ref-editorial",
            )
        )
        with pytest.raises(CasError, match="approval-target-mismatch") as mismatch:
            apply_transition(
                store,
                "job-1",
                expected_status="PREVIEW_READY",
                expected_parent_hash=parent,
                new_status="EDITORIAL_APPROVED",
                new_artifact_hash=synthetic_hash("approved-plan"),
                payload=wrong_target,
            )
        assert mismatch.value.code == "approval-target-mismatch"

        checkpoint = TransitionPayload(
            approval=ApprovalRef(
                purpose=APPROVAL_PURPOSE_EDITORIAL,
                target_hash=parent,
                artifact_ref="operator-checkpoint-h1",
            )
        )
        result = apply_transition(
            store,
            "job-1",
            expected_status="PREVIEW_READY",
            expected_parent_hash=parent,
            new_status="EDITORIAL_APPROVED",
            new_artifact_hash=synthetic_hash("approved-plan"),
            payload=checkpoint,
        )
        assert result.status == "EDITORIAL_APPROVED"
        refs = store.get_approval_refs()
        assert [(ref.purpose, ref.target_hash, ref.artifact_ref) for ref in refs] == [
            (APPROVAL_PURPOSE_EDITORIAL, parent, "operator-checkpoint-h1")
        ]


def test_auxiliary_self_transitions(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        drive_to(store, "ANALYZED")
        before = current_job_state(store, "job-1")

        retry_hash = synthetic_hash("analyzed-rerun")
        retry = apply_transition(
            store,
            "job-1",
            expected_status="ANALYZED",
            expected_parent_hash=before.adopted_artifact_hash,
            new_status="ANALYZED",
            new_artifact_hash=retry_hash,
        )
        assert (retry.status, retry.adopted_artifact_hash) == ("ANALYZED", retry_hash)
        assert edge_for("ANALYZED", "ANALYZED") is not None

        replay = apply_transition(
            store,
            "job-1",
            expected_status="ANALYZED",
            expected_parent_hash=retry_hash,
            new_status="ANALYZED",
            new_artifact_hash=retry_hash,
        )
        assert replay == retry

        recommit = apply_transition(
            store,
            "job-1",
            expected_status="ANALYZED",
            expected_parent_hash=retry_hash,
            new_status="ANALYZED",
            new_artifact_hash=synthetic_hash("analyzed-recommit"),
        )
        assert recommit.adopted_artifact_hash == synthetic_hash("analyzed-recommit")
        assert current_job_state(store, "job-1").status == "ANALYZED"


def test_self_transition_allowed_across_non_frozen_statuses(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        for status in ("PLAN_COMMITTED", "PREVIEW_READY"):
            drive_to(store, status)
            current = cast("JobStatus", status)
            result = apply_transition(
                store,
                "job-1",
                expected_status=current,
                expected_parent_hash=current_job_state(store, "job-1").adopted_artifact_hash,
                new_status=current,
                new_artifact_hash=synthetic_hash(f"self-{status}"),
            )
            assert result.status == status


def test_superseded_check_precedes_legality(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        drive_to(store, "INGESTED")

        with pytest.raises(CasError, match="superseded") as error:
            apply_transition(
                store,
                "job-1",
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash=synthetic_hash("stale-ingest"),
            )
        assert error.value.code == "superseded"
        assert current_job_state(store, "job-1").status == "INGESTED"


def test_missing_job_and_bad_hash_fail_closed(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        with pytest.raises(StateStoreError, match="job-missing"):
            apply_transition(
                store,
                "job-none",
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash=synthetic_hash("ingest"),
            )
        with pytest.raises(CasError, match="invalid-hash"):
            apply_transition(
                store,
                "job-1",
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash="not-a-hash",
            )
