"""commit_restore / restore_payload semantics (UX redesign 工程1).

Store-machinery counterpart of the cockpit revert endpoint tests: a
restore is a NEW forward version whose content equals the restored-from
version's, recorded by ONE self-contained ``plan_restored`` event —
history is never rewritten and no AppliedCommand is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from services.review_command.commit import commit_command
from services.review_command.events import (
    RESTORED_EVENT_KIND,
    event_restored_plan,
    restore_payload,
)
from services.review_command.reducer import reduce
from services.review_command.restore import build_restore_event, commit_restore
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    initialize_store,
    load_head,
    load_version_plan,
)
from tests.review_command.support import fixture_proposal, manifest_plan


class StorePaths(NamedTuple):
    log_path: Path
    plan_dir: Path


@pytest.fixture
def applied_store(tmp_path: Path) -> StorePaths:
    """A store whose head is v2 (one applied remove), so v1 is restorable."""

    paths = StorePaths(log_path=tmp_path / "events.jsonl", plan_dir=tmp_path / "store")
    initialize_store(manifest_plan("p0c-remove-clear"), paths.log_path, paths.plan_dir)
    commit_command(
        fixture_proposal("p0c-span-clear"),
        OperatorDecision0C(decision_id="dec-001", actor_intent="operator"),
        paths.log_path,
        paths.plan_dir,
    )
    return paths


def _decision() -> OperatorDecision0C:
    return OperatorDecision0C(
        decision_id="dec-revert-v2",
        actor_intent="operator",
        note="restore the previous version",
    )


def test_restore_commits_parent_content_as_new_plan_restored_version(
    applied_store: StorePaths,
) -> None:
    outcome = commit_restore(1, _decision(), applied_store.log_path, applied_store.plan_dir)
    assert outcome.deferred is False
    assert outcome.version == 3
    head = load_head(applied_store.log_path, applied_store.plan_dir)
    assert head.version == 3
    assert (applied_store.plan_dir / "plan-v3.json").read_bytes() == (
        applied_store.plan_dir / "plan-v1.json"
    ).read_bytes()
    event = head.events[-1]
    assert event.kind == RESTORED_EVENT_KIND
    assert event.base_plan_version == "v2"
    assert event.result_plan_version == "v3"
    assert event.decision_id is not None
    assert event.applied is True


def test_restore_folds_back_to_the_restored_plan_without_disk_reads(
    applied_store: StorePaths,
) -> None:
    outcome = commit_restore(1, _decision(), applied_store.log_path, applied_store.plan_dir)
    head = load_head(applied_store.log_path, applied_store.plan_dir)
    parent = load_version_plan(applied_store.plan_dir, head.index, 1)
    assert head.plan == parent
    folded = reduce(head.events, head.base_plan)
    assert folded.plan == head.plan
    assert folded.plan == parent
    assert outcome.event_id == head.events[-1].event_id


def test_restore_payload_round_trips_through_event_parser() -> None:
    event = build_restore_event(
        sequence=2,
        base_plan_version="v2",
        result_plan_version="v3",
        previous_event_hash="0" * 64,
        actor_intent="operator",
        decision_id="dec-revert-v2",
        restored_plan_json=restore_payload(1, manifest_plan("p0c-remove-clear")),
    )
    assert event_restored_plan(event) == manifest_plan("p0c-remove-clear")


def test_restore_refuses_out_of_range_source_version(applied_store: StorePaths) -> None:
    with pytest.raises(ReviewCommitError) as below_range:
        commit_restore(0, _decision(), applied_store.log_path, applied_store.plan_dir)
    assert below_range.value.code == "restore-version-invalid"
    with pytest.raises(ReviewCommitError) as at_head:
        commit_restore(2, _decision(), applied_store.log_path, applied_store.plan_dir)
    assert at_head.value.code == "restore-version-invalid"


def test_restore_refuses_model_authored_decision(applied_store: StorePaths) -> None:
    model_decision = OperatorDecision0C(
        decision_id="dec-model", actor_intent="model", note="not allowed"
    )
    with pytest.raises(ReviewCommitError) as raised:
        commit_restore(1, model_decision, applied_store.log_path, applied_store.plan_dir)
    assert raised.value.code == "model_authored_decision"
