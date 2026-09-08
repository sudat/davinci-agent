"""Saved proposal sets are the ONLY adoption authority (brief §5.3, P0).

Regression set for the codex finding: a browser-fabricated draft
(recomputed command_id, forged ``investigated``/target fields) must never
mutate the plan. Every apply is authorized by FULL field-for-field
equality against the SERVER-SAVED, unconsumed proposal set of one chat
entry, with the plan head unchanged since the preview.
"""

# allow: SIZE_OK — one codex-attack regression concern per file (fabricated
# draft shapes x the authorization contract); splitting the attack matrix
# across files would scatter one adversarial story.

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.models import (
    ReviewChatEntry,
    ReviewProposalConsumed,
)
from services.episode_cockpit.review_chat import (
    ReviewChatContext,
    ReviewStoreLocation,
    _command_id,
)
from services.episode_cockpit.review_proposals import ReviewProposalSet
from services.review_command.store import initialize_store, load_head
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.episode_cockpit.review_chat import ReviewCommandKind
    from services.episode_cockpit.review_interpreter import NearbyContext

REMOVE_TEXT = "この区間を削除して"
KEEP_TEXT = "この後2秒残して"
AT = 6.0


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {"state_store": tmp_path / "state.db", "episodes_root": tmp_path / "jobs"}


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"], episodes_root=workspace["episodes_root"]
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


def _store_of(workspace: dict[str, Path], episode_id: str) -> ReviewStoreLocation:
    return ReviewStoreLocation(
        log_path=workspace["episodes_root"] / episode_id / "review" / "events.jsonl",
        plan_dir=workspace["episodes_root"] / episode_id / "review" / "store",
    )


def _seed_store(workspace: dict[str, Path], episode_id: str) -> None:
    store = _store_of(workspace, episode_id)
    initialize_store(manifest_plan("p0c-remove-clear"), store.log_path, store.plan_dir)


def _preview(client: TestClient, episode_id: str, text: str) -> dict[str, object]:
    response = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": text, "at_seconds": AT}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, episode_id: str, payload: dict[str, object]):
    return client.post(f"/episodes/{episode_id}/review-chat/apply", json=payload)


def _attack_draft(preview: dict[str, object], **mutations: object) -> dict[str, object]:
    """Fabricate a draft that self-consistently re-derives its command_id."""

    draft: dict[str, object] = dict(cast("dict[str, object]", preview["draft"]))
    draft.update(mutations)
    draft["command_id"] = _command_id(
        cast("ReviewCommandKind | None", draft["command_kind"]),
        cast("float | None", draft["target_seconds"]),
        cast("float | None", draft["seconds_delta"]),
        cast("str", draft["text"]),
    )
    return draft


def test_fabricated_investigated_draft_with_matching_command_id_is_rejected(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    forged = _attack_draft(
        _preview(client, episode_id, REMOVE_TEXT),
        investigated=True,
        needs_confirmation=True,
        confirmation_reason="forged investigation",
    )
    response = _apply(
        client, episode_id,
        {"text": REMOVE_TEXT, "at_seconds": AT, "drafts": [forged], "sequence": 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "proposal-mismatch"
    store = _store_of(workspace, episode_id)
    assert load_head(store.log_path, store.plan_dir).version == 1


def test_field_tamper_vs_saved_set_is_rejected(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    preview = _preview(client, episode_id, REMOVE_TEXT)
    for mutated in (
        _attack_draft(preview, target_seconds=90.0),
        _attack_draft(preview, hypothesis="偽の仮説"),
        _attack_draft(preview, scope="channel"),
    ):
        response = _apply(
            client, episode_id,
            {"text": REMOVE_TEXT, "at_seconds": AT, "drafts": [mutated], "sequence": 1},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "proposal-mismatch"


def test_apply_without_prior_chat_is_proposal_not_found(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    plain = _apply(client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT})
    assert plain.status_code == 422
    assert plain.json()["error"]["code"] == "proposal-not-found"
    unknown_sequence = _apply(
        client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT, "sequence": 99}
    )
    assert unknown_sequence.status_code == 422
    assert unknown_sequence.json()["error"]["code"] == "proposal-not-found"


def test_apply_after_head_moved_is_proposal_stale_409(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    _preview(client, episode_id, REMOVE_TEXT)
    _preview(client, episode_id, KEEP_TEXT)
    first = _apply(client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT, "sequence": 1})
    assert first.status_code == 200, first.text
    stale = _apply(client, episode_id, {"text": KEEP_TEXT, "at_seconds": AT, "sequence": 2})
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "proposal-stale"
    assert "v2" in error["detail"]
    old_shape = _apply(client, episode_id, {"text": KEEP_TEXT, "at_seconds": AT})
    assert old_shape.status_code == 409
    assert old_shape.json()["error"]["code"] == "proposal-stale"


def test_resend_after_successful_apply_is_proposal_consumed(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    _preview(client, episode_id, REMOVE_TEXT)
    payload: dict[str, object] = {"text": REMOVE_TEXT, "at_seconds": AT, "sequence": 1}
    assert _apply(client, episode_id, payload).status_code == 200
    resent = _apply(client, episode_id, payload)
    assert resent.status_code == 422
    assert resent.json()["error"]["code"] == "proposal-consumed"
    old_client = _apply(client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT})
    assert old_client.status_code == 422
    assert old_client.json()["error"]["code"] == "proposal-consumed"


def test_cross_episode_sequence_reference_is_rejected(
    client: TestClient, workspace: dict[str, Path], source_folder: Path, tmp_path: Path
) -> None:
    episode_one = _create_episode(client, source_folder)
    _seed_store(workspace, episode_one)
    _preview(client, episode_one, REMOVE_TEXT)
    folder_b = tmp_path / "cam-b"
    folder_b.mkdir()
    (folder_b / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    episode_two = client.post(
        "/episodes", json={"source_folder": str(folder_b), "brief_text": "b"}
    ).json()["episode_id"]
    _seed_store(workspace, str(episode_two))
    response = _apply(
        client, str(episode_two), {"text": REMOVE_TEXT, "at_seconds": AT, "sequence": 1}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "proposal-not-found"


def test_preview_apply_round_trip_for_direct_and_investigated(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_llm(text: str, context: ReviewChatContext, nearby: NearbyContext) -> dict:
        del text, context, nearby
        return {
            "proposals": [
                {"command_kind": "remove_section", "target_seconds": 12.0, "hypothesis_ja": "仮説"}
            ]
        }

    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: fake_llm
    )
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    _preview(client, episode_id, REMOVE_TEXT)
    direct = _apply(client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT, "sequence": 1})
    assert direct.status_code == 200, direct.text
    assert direct.json()["applied"]["result_plan_version"] == "v2"
    feelings = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": "ここ退屈", "at_seconds": 12.0}
    )
    assert feelings.status_code == 200
    feelings_draft = feelings.json()["draft"]
    feelings_apply = _apply(
        client,
        episode_id,
        {"text": "ここ退屈", "at_seconds": 12.0, "drafts": [feelings_draft], "sequence": 2},
    )
    assert feelings_apply.status_code == 200, feelings_apply.text
    assert feelings_apply.json()["applied"]["result_plan_version"] == "v3"


def test_old_client_shape_applies_after_preview(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    _preview(client, episode_id, REMOVE_TEXT)
    response = _apply(client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT})
    assert response.status_code == 200, response.text
    assert response.json()["applied"]["result_plan_version"] == "v2"


def test_saved_set_file_records_base_version_and_consumption_marker(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = workspace["episodes_root"] / episode_id
    _seed_store(workspace, episode_id)
    _preview(client, episode_id, REMOVE_TEXT)
    proposals = [
        json.loads(line)
        for line in (episode_dir / "review-proposals.jsonl").read_bytes().splitlines()
    ]
    assert proposals[0]["schema_version"] == "cockpit-review-proposals-v1"
    assert proposals[0]["chat_sequence"] == 1
    assert proposals[0]["base_plan_version"] == "v1"
    response = _apply(
        client, episode_id, {"text": REMOVE_TEXT, "at_seconds": AT, "sequence": 1}
    )
    assert response.status_code == 200
    consumed = [
        json.loads(line)
        for line in (episode_dir / "review-proposals-consumed.jsonl").read_bytes().splitlines()
    ]
    assert [entry["set_sequence"] for entry in consumed] == [1]
    assert consumed[0]["schema_version"] == "cockpit-review-proposal-consumed-v1"


def test_old_records_without_new_fields_still_parse() -> None:
    """工程2 additive fields: old chat entries / proposal sets / consumption
    records (written before reaction/outcome/linkage fields) still parse."""

    chat = ReviewChatEntry.model_validate(
        {"schema_version": "cockpit-review-chat-v1", "sequence": 1, "text": "古い入力"}
    )
    assert chat.reaction is None
    assert chat.in_response_to_set is None
    proposal = ReviewProposalSet.model_validate(
        {
            "schema_version": "cockpit-review-proposals-v1",
            "sequence": 1,
            "chat_sequence": 1,
            "drafts": [],
            "created_at": "t",
        }
    )
    assert proposal.responds_to_set is None
    assert proposal.reaction_kind is None
    consumed = ReviewProposalConsumed.model_validate(
        {
            "schema_version": "cockpit-review-proposal-consumed-v1",
            "sequence": 1,
            "set_sequence": 1,
            "created_at": "t",
        }
    )
    assert consumed.outcome is None  # absent outcome = applied (legacy)
