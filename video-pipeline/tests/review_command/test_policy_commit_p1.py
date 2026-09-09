"""Slice2 P1 (step 5): policy-commit CAS, idempotency, and orphan recovery.

Store-level proofs for ``commit_policy``: identical re-commits return the
sealed event idempotently, same-judgment different-plan re-commits refuse
before writing, stale expected bases write nothing, and mid-commit file
failures recover the orphan (or honestly name the recorded version).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services import foundation_io
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.foundation_io import canonical_model_bytes
from services.review_command import policy_commit
from services.review_command.events import POLICY_EVENT_KIND
from services.review_command.policy_commit import (
    commit_policy,
    locked_field_conflicts,
    reuse_committed_policy,
)
from services.review_command.store import (
    INDEX_NAME,
    OperatorDecision0C,
    PlanVersionsIndex,
    ReviewCommitError,
    initialize_store,
    load_events,
    load_head,
    load_index,
)

RATE = RationalFrameRate(num=30, den=1)


def _seed_plan(*, artifact_id: str = "edit-plan-review-ep-seed") -> EditPlan0C:
    def video(item_id: str, start: int, end: int) -> EditPlanItem0C:
        return EditPlanItem0C(
            item_id=item_id,
            kind="video",
            source_id="src-ep",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
            track_index=1,
        )

    return EditPlan0C(
        artifact_id=artifact_id,
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="phase1-review-plane", version="1"),
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="src-ep", total_frames=60),
            items=(video("v1", 0, 30), video("v2", 30, 60)),
        ),
    )


def _store(tmp_path: Path) -> tuple[Path, Path]:
    log_path = tmp_path / "review" / "events.jsonl"
    plan_dir = tmp_path / "review" / "store"
    plan_dir.mkdir(parents=True)
    initialize_store(_seed_plan(), log_path, plan_dir)
    return log_path, plan_dir


def _decision() -> OperatorDecision0C:
    return OperatorDecision0C(
        decision_id="dec-policy-j1", actor_intent="operator", note="adopt j1"
    )


def _policy_events(log_path: Path, plan_dir: Path) -> list:
    return [
        event
        for event in load_events(log_path)
        if event.kind == POLICY_EVENT_KIND
    ]


def _fail_once_on(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = foundation_io.atomic_write
    calls = {"count": 0}

    def fake(path: Path, payload: bytes) -> None:
        if path.name == name and calls["count"] == 0:
            calls["count"] += 1
            raise OSError(f"injected failure for {name}")
        real(path, payload)

    monkeypatch.setattr(policy_commit, "atomic_write", fake)


def test_commit_policy_same_linkage_and_plan_returns_original_idempotently(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _store(tmp_path)
    plan = load_head(log_path, plan_dir).plan.model_copy(
        update={"artifact_id": "edit-plan-policy-v2"}
    )

    first = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    log_bytes = log_path.read_bytes()

    second = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )

    assert first.version == 2
    assert first.idempotent is False
    assert second.version == 2
    assert second.idempotent is True
    assert second.event_id == first.event_id
    assert log_path.read_bytes() == log_bytes
    assert len(_policy_events(log_path, plan_dir)) == 1


def test_commit_policy_same_judgment_with_different_plan_refuses(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _store(tmp_path)
    head = load_head(log_path, plan_dir)
    plan_a = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-a"})
    plan_b = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-b"})

    commit_policy(
        plan_a, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    log_bytes = log_path.read_bytes()

    with pytest.raises(ReviewCommitError) as exc_info:
        commit_policy(
            plan_b, _decision(), log_path, plan_dir,
            judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
        )

    assert exc_info.value.code == "policy-idempotency-conflict"
    assert log_path.read_bytes() == log_bytes
    assert len(_policy_events(log_path, plan_dir)) == 1


def test_commit_policy_expected_base_mismatch_writes_nothing(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _store(tmp_path)
    head = load_head(log_path, plan_dir)
    base_sha = head.index.versions["1"].plan_sha256
    plan_v2 = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v2"})
    commit_policy(
        plan_v2, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    plan_v3 = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v3"})
    snapshot = {
        name: (plan_dir / name).read_bytes()
        for name in ("plan-v1.json", "ir-v1.json", "plan-v2.json", "ir-v2.json", INDEX_NAME)
    }
    events_bytes = log_path.read_bytes()
    seal_bytes = log_path.parent.joinpath(f"{log_path.name}.seal").read_bytes()

    with pytest.raises(ReviewCommitError) as exc_info:
        commit_policy(
            plan_v3, _decision(), log_path, plan_dir,
            judgment_id="j2", proposal_id="prop-1", policy_decision="adopt",
            expected_base_version="v1",
            expected_base_plan_sha256=base_sha,
        )

    assert exc_info.value.code == "policy-base-version-changed"
    assert log_path.read_bytes() == events_bytes
    assert log_path.parent.joinpath(f"{log_path.name}.seal").read_bytes() == seal_bytes
    for name, payload in snapshot.items():
        assert (plan_dir / name).read_bytes() == payload
    assert not (plan_dir / "plan-v3.json").exists()
    assert not (plan_dir / "ir-v3.json").exists()


def test_policy_plan_write_failure_recovers_orphan_and_returns_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path, plan_dir = _store(tmp_path)
    plan = load_head(log_path, plan_dir).plan.model_copy(
        update={"artifact_id": "edit-plan-policy-v2"}
    )
    _fail_once_on(monkeypatch, "plan-v2.json")

    outcome = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )

    assert outcome.version == 2
    assert outcome.idempotent is True
    assert (plan_dir / "plan-v2.json").is_file()
    assert (plan_dir / "ir-v2.json").is_file()
    events = _policy_events(log_path, plan_dir)
    assert len(events) == 1
    assert outcome.event_id == events[0].event_id
    assert load_head(log_path, plan_dir).version == 2


def test_policy_ir_write_failure_recovers_without_duplicate_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path, plan_dir = _store(tmp_path)
    plan = load_head(log_path, plan_dir).plan.model_copy(
        update={"artifact_id": "edit-plan-policy-v2"}
    )
    _fail_once_on(monkeypatch, "ir-v2.json")

    outcome = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )

    assert outcome.version == 2
    assert outcome.idempotent is True
    assert len(_policy_events(log_path, plan_dir)) == 1
    assert load_head(log_path, plan_dir).version == 2


def test_policy_index_write_failure_recovers_without_v3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path, plan_dir = _store(tmp_path)
    plan = load_head(log_path, plan_dir).plan.model_copy(
        update={"artifact_id": "edit-plan-policy-v2"}
    )
    _fail_once_on(monkeypatch, INDEX_NAME)

    outcome = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )

    assert outcome.version == 2
    assert outcome.idempotent is True
    assert not (plan_dir / "plan-v3.json").exists()
    assert not (plan_dir / "ir-v3.json").exists()
    assert load_head(log_path, plan_dir).version == 2


def test_crash_after_policy_event_then_retry_recovers_idempotently(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _store(tmp_path)
    plan = load_head(log_path, plan_dir).plan.model_copy(
        update={"artifact_id": "edit-plan-policy-v2"}
    )
    first = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    assert first.version == 2
    (plan_dir / "plan-v2.json").unlink()
    (plan_dir / "ir-v2.json").unlink()
    index = load_index(plan_dir)
    stripped = PlanVersionsIndex(
        schema_version="plan-versions-v1",
        base_artifact_id=index.base_artifact_id,
        versions={key: value for key, value in index.versions.items() if key == "1"},
    )
    foundation_io.atomic_write(
        plan_dir / INDEX_NAME, canonical_model_bytes(stripped)
    )
    with pytest.raises(ReviewCommitError):
        load_head(log_path, plan_dir)

    second = commit_policy(
        plan, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )

    assert second.version == 2
    assert second.idempotent is True
    assert second.event_id == first.event_id
    assert len(_policy_events(log_path, plan_dir)) == 1
    assert (plan_dir / "plan-v2.json").is_file()
    assert (plan_dir / "ir-v2.json").is_file()
    assert load_head(log_path, plan_dir).version == 2


def _locked_seed_plan() -> EditPlan0C:
    plan = _seed_plan()
    items = tuple(
        item.model_copy(update={"locked_fields": ("order",)})
        if item.item_id == "v1"
        else item
        for item in plan.plan.items
    )
    return plan.model_copy(update={"plan": plan.plan.model_copy(update={"items": items})})


def _locked_store(tmp_path: Path) -> tuple[Path, Path]:
    log_path = tmp_path / "review" / "events.jsonl"
    plan_dir = tmp_path / "review" / "store"
    plan_dir.mkdir(parents=True)
    initialize_store(_locked_seed_plan(), log_path, plan_dir)
    return log_path, plan_dir


def _reversed_plan(head_plan: EditPlan0C) -> EditPlan0C:
    items = tuple(
        item.model_copy(update={"locked_fields": ()})
        for item in reversed(head_plan.plan.items)
    )
    return _with_items(head_plan, items, "edit-plan-policy-reversed")


def _with_items(
    head_plan: EditPlan0C,
    items: tuple[EditPlanItem0C, ...],
    artifact_id: str,
) -> EditPlan0C:
    return head_plan.model_copy(
        update={
            "artifact_id": artifact_id,
            "plan": head_plan.plan.model_copy(update={"items": items}),
        }
    )


def test_w2_locked_order_reversal_rejected_with_correct_cas(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _locked_store(tmp_path)
    head = load_head(log_path, plan_dir)
    base_sha = head.index.versions["1"].plan_sha256
    events_bytes = log_path.read_bytes()

    with pytest.raises(ReviewCommitError) as exc_info:
        commit_policy(
            _reversed_plan(head.plan), _decision(), log_path, plan_dir,
            judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
            expected_base_version="v1",
            expected_base_plan_sha256=base_sha,
        )

    assert exc_info.value.code == "policy-locked-field-conflict"
    assert "v1.order" in str(exc_info.value)
    assert load_head(log_path, plan_dir).version == 1
    assert log_path.read_bytes() == events_bytes
    assert not (plan_dir / "plan-v2.json").exists()


def test_w2_dropped_locks_rejected_even_when_order_kept(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _locked_store(tmp_path)
    head = load_head(log_path, plan_dir)
    stripped = _with_items(
        head.plan,
        tuple(
            item.model_copy(update={"locked_fields": ()})
            for item in head.plan.plan.items
        ),
        "edit-plan-policy-stripped",
    )

    with pytest.raises(ReviewCommitError) as exc_info:
        commit_policy(
            stripped, _decision(), log_path, plan_dir,
            judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
        )

    assert exc_info.value.code == "policy-locked-field-conflict"
    assert "v1.locks" in str(exc_info.value)
    assert load_head(log_path, plan_dir).version == 1


def test_w2_unlocked_change_commits_and_locks_propagate(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _locked_store(tmp_path)
    head = load_head(log_path, plan_dir)
    items = tuple(
        item.model_copy(
            update={
                "span": item.span.model_copy(
                    update={"end_frame": item.span.end_frame - 5}
                )
            }
        )
        if item.item_id == "v2"
        else item
        for item in head.plan.plan.items
    )
    changed = _with_items(head.plan, items, "edit-plan-policy-v2")

    committed = commit_policy(
        changed, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )

    assert committed.version == 2
    assert committed.superseded_by_head is False
    kept = {
        item.item_id: item for item in load_head(log_path, plan_dir).plan.plan.items
    }
    assert "order" in kept["v1"].locked_fields
    with pytest.raises(ReviewCommitError) as exc_info:
        commit_policy(
            _reversed_plan(load_head(log_path, plan_dir).plan),
            _decision(), log_path, plan_dir,
            judgment_id="j2", proposal_id="prop-1", policy_decision="adopt",
        )
    assert exc_info.value.code == "policy-locked-field-conflict"


def test_w2_locked_field_conflicts_unit_shape() -> None:
    plan = _locked_seed_plan()
    assert locked_field_conflicts(plan, plan) == ()
    assert "v1.order" in locked_field_conflicts(plan, _reversed_plan(plan))


def test_w10_idempotent_return_marks_superseded_when_head_advanced(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _store(tmp_path)
    head = load_head(log_path, plan_dir)
    plan_a = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-a"})
    plan_b = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-b"})

    first_a = commit_policy(
        plan_a, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    commit_policy(
        plan_b, _decision(), log_path, plan_dir,
        judgment_id="j2", proposal_id="prop-1", policy_decision="adopt",
    )
    assert load_head(log_path, plan_dir).version == 3

    repeat_a = commit_policy(
        plan_a, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    repeat_b = commit_policy(
        plan_b, _decision(), log_path, plan_dir,
        judgment_id="j2", proposal_id="prop-1", policy_decision="adopt",
    )

    assert first_a.superseded_by_head is False
    assert repeat_a.version == 2
    assert repeat_a.idempotent is True
    assert repeat_a.superseded_by_head is True
    assert repeat_a.event_id == first_a.event_id
    assert repeat_b.version == 3
    assert repeat_b.superseded_by_head is False
    assert load_head(log_path, plan_dir).version == 3
    assert len(_policy_events(log_path, plan_dir)) == 2


def test_w10_reuse_committed_policy_marks_superseded_record(
    tmp_path: Path,
) -> None:
    log_path, plan_dir = _store(tmp_path)
    head = load_head(log_path, plan_dir)
    plan_a = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-a"})
    plan_b = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-b"})
    commit_policy(
        plan_a, _decision(), log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    commit_policy(
        plan_b, _decision(), log_path, plan_dir,
        judgment_id="j2", proposal_id="prop-1", policy_decision="adopt",
    )

    reused_old = reuse_committed_policy(log_path, plan_dir, "j1")
    reused_head = reuse_committed_policy(log_path, plan_dir, "j2")

    assert reused_old is not None
    assert reused_old.superseded_by_head is True
    assert reused_old.version == 2
    assert reused_head is not None
    assert reused_head.superseded_by_head is False
    assert reused_head.version == 3
