"""工程2P: episode_status completion markers — complementary cases.

The 未消費 proposal-set marker counts only a set whose base plan
version still equals the CURRENT head; this file pins the case
``test_run_visibility.py`` does not: an UNCONSUMED set whose base is an
OLD version while the head has moved does NOT count (stale, not
pending). ``preview_first_arrived_at`` comes from the current run's
preview row first-arrival (derivable via the runner_started fallback
when no rebuild record exists) and labels 試し編集完了 WITHOUT changing
job semantics — status stays PREVIEW_READY.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.models import ReviewProposalConsumed
from services.episode_cockpit.review_chat import ReviewCommandDraft, _command_id
from services.episode_cockpit.review_proposals import (
    REVIEW_PROPOSALS_CONSUMED_NAME,
    REVIEW_PROPOSALS_NAME,
    ReviewProposalSet,
)
from services.foundation_io import canonical_model_bytes
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

T2 = "2026-09-08T10:00:01+00:00"
REMOVE_TEXT = "この区間を削除して"
FEELINGS_AT = 6.0


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {"state_store": tmp_path / "state.db", "episodes_root": tmp_path / "jobs"}


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def source_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "travel vlog"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_review_store(workspace: dict[str, Path], episode_id: str) -> None:
    episode_dir = workspace["episodes_root"] / episode_id
    initialize_store(
        manifest_plan("p0c-remove-clear"),
        episode_dir / "review" / "events.jsonl",
        episode_dir / "review" / "store",
    )


def _apply_remove(client: TestClient, episode_id: str) -> None:
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": REMOVE_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert preview.status_code == 200
    applied = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": REMOVE_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert applied.status_code == 200, applied.text  # head is now v2


def _draft() -> ReviewCommandDraft:
    return ReviewCommandDraft.model_validate(
        {
            "command_id": _command_id("remove_section", 0.5, None, REMOVE_TEXT),
            "command_kind": "remove_section",
            "text": REMOVE_TEXT,
            "target_seconds": 0.5,
            "seconds_delta": None,
            "scope": "episode",
            "needs_confirmation": False,
            "confirmation_reason": None,
        }
    )


def _append_line(path: Path, entry: ReviewProposalSet | ReviewProposalConsumed) -> None:
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(entry) + b"\n")


def _set(sequence: int, base: str) -> ReviewProposalSet:
    return ReviewProposalSet(
        sequence=sequence, chat_sequence=sequence, base_plan_version=base,
        drafts=(_draft(),), created_at=T2,
    )


def test_unconsumed_set_on_old_head_version_does_not_count(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_review_store(workspace, episode_id)
    episode_dir = workspace["episodes_root"] / episode_id
    sets_path = episode_dir / REVIEW_PROPOSALS_NAME
    _append_line(sets_path, _set(1, "v1"))
    _append_line(sets_path, _set(2, "v1"))  # stays UNCONSUMED on purpose

    body = client.get(f"/episodes/{episode_id}").json()
    assert body["unreviewed_proposal_set"] is True  # head v1, sets on v1

    _apply_remove(client, episode_id)  # consumes the chat-created set; head → v2
    body = client.get(f"/episodes/{episode_id}").json()
    assert body["unreviewed_proposal_set"] is False  # unconsumed v1 set is STALE

    _append_line(sets_path, _set(4, "v2"))
    body = client.get(f"/episodes/{episode_id}").json()
    assert body["unreviewed_proposal_set"] is True  # set on the CURRENT version

    _append_line(
        episode_dir / REVIEW_PROPOSALS_CONSUMED_NAME,
        ReviewProposalConsumed(sequence=1, set_sequence=4, created_at=T2, outcome="applied"),
    )
    body = client.get(f"/episodes/{episode_id}").json()
    assert body["unreviewed_proposal_set"] is False  # consumed ⇒ nothing on the table


def test_preview_first_arrival_labels_without_status_change(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    intake_created_at = json.loads(
        (workspace["episodes_root"] / episode_id / "intake.json").read_bytes()
    )["created_at"]
    (workspace["episodes_root"] / episode_id / "runner.log").write_text(
        json.dumps({"ts": T2, "event": "runner_started", "run_id": "newrun02"}) + "\n",
        encoding="utf-8",
    )
    with StateStore.open(workspace["state_store"]) as store:
        store.record_stage_run(
            StageRunRow(
                job_id=episode_id,
                stage_name="preview",
                input_artifact_hashes=(),
                status="succeeded",
                idempotency_key="cockpit-episode-runner-v1:newrun02:preview",
                run_id="newrun02",
                first_output_arrived_at=T2,
                last_transition_at=T2,
            )
        )
        for index, job_status in enumerate(
            ("INGESTED", "NORMALIZED", "ANALYZED", "PLAN_PROPOSED", "PLAN_COMMITTED",
             "PREVIEW_READY"),
            start=1,
        ):
            snapshot = current_job_state(store, episode_id)
            apply_transition(
                store, episode_id, expected_status=snapshot.status,
                expected_parent_hash=snapshot.adopted_artifact_hash,
                new_status=job_status, new_artifact_hash=f"{index:064x}",
            )

    body = client.get(f"/episodes/{episode_id}").json()

    assert body["status"] == "PREVIEW_READY"  # job semantics UNCHANGED
    assert body["current_run"] == "newrun02"  # runner_started fallback
    assert body["intake_created_at"] == intake_created_at
    assert body["preview_first_arrived_at"] == T2  # labels 試し編集完了 only
