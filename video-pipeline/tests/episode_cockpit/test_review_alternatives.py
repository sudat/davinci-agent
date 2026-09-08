"""Alternatives proposal sets (工程2 rework): mutually exclusive, one adopted.

「互いに選択する代替案」 is a set of at most TWO competing directions —
「両方違う」 re-investigation is what produces it. Echoing the whole set
(apply-all) is a typed 422: contradictory proposals must never be applied
together. Adopting ONE consumes the set and moves the base, so the other
alternative is never silently applicable afterwards. The command-bundle
counterpart lives in test_review_command_bundle.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.review_chat import ReviewChatContext, ReviewStoreLocation
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.episode_cockpit.review_interpreter import NearbyContext

FEELINGS = "全体が素人っぽい"
BOTH_DIFFERENT = "両方違う"
CHOICE_B = "Bが好き"


def _proposal(target: float) -> dict[str, object]:
    return {"command_kind": "remove_section", "target_seconds": target}


def _llm(*returns: list[dict]) -> object:
    state = {"n": 0}

    def call(text: str, context: ReviewChatContext, nearby: NearbyContext) -> dict:
        del text, nearby
        state["n"] += 1
        return {"proposals": list(returns[min(state["n"] - 1, len(returns) - 1)])}

    return call


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


def _episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "travel vlog"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed(workspace: dict[str, Path], episode_id: str) -> None:
    base = workspace["episodes_root"] / episode_id / "review"
    store = ReviewStoreLocation(log_path=base / "events.jsonl", plan_dir=base / "store")
    initialize_store(manifest_plan("p0c-remove-clear"), store.log_path, store.plan_dir)


def _chat(client: TestClient, episode_id: str, text: str) -> dict[str, object]:
    response = client.post(f"/episodes/{episode_id}/review-chat", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, episode_id: str, payload: dict[str, object]):
    return client.post(f"/episodes/{episode_id}/review-chat/apply", json=payload)


def _jsonl(workspace: dict[str, Path], episode_id: str, name: str) -> list[dict[str, object]]:
    log = workspace["episodes_root"] / episode_id / name
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_bytes().splitlines()]


def _drafts_of(body: dict[str, object]) -> list[dict[str, object]]:
    """The (possibly absent) multi-draft list of a chat response, typed."""

    return cast("list[dict[str, object]]", body.get("drafts", []))


def _alternatives_set(
    client: TestClient,
    workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    source_folder: Path,
    alternatives: list[dict],
) -> tuple[str, dict[str, object]]:
    """An ALTERNATIVES set as set 2 (set 1 = feelings bundle, rejected)."""

    fake = _llm([_proposal(12.0)], alternatives)  # ONE closure: state survives chats
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _chat(client, episode_id, FEELINGS)
    second = _chat(client, episode_id, BOTH_DIFFERENT)
    return episode_id, second


def test_alternatives_route_serves_at_most_two(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    three = [_proposal(12.0), _proposal(6.0), _proposal(3.0)]
    fake = _llm([_proposal(12.0)], three)  # ONE closure: state survives chats
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _chat(client, episode_id, FEELINGS)
    second = _chat(client, episode_id, BOTH_DIFFERENT)
    assert len(_drafts_of(second)) == 2  # 代替案は最大2 (journal on the slice)
    assert second["proposal_kind"] == "alternatives"
    sets = _jsonl(workspace, episode_id, "review-proposals.jsonl")
    assert sets[1]["proposal_kind"] == "alternatives"


def test_apply_all_on_alternatives_is_typed_error(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, second = _alternatives_set(
        client, workspace, monkeypatch, source_folder, [_proposal(12.0), _proposal(6.0)]
    )
    applied = _apply(
        client, episode_id, {"text": BOTH_DIFFERENT, "drafts": _drafts_of(second), "sequence": 2}
    )
    assert applied.status_code == 422
    assert applied.json()["error"]["code"] == "alternatives-require-choice"
    consumed = _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl")
    assert [entry["outcome"] for entry in consumed] == ["rejected"]  # only set 1


def test_choice_on_single_draft_alternatives_is_typed_error(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, _ = _alternatives_set(
        client, workspace, monkeypatch, source_folder, [_proposal(6.0)]
    )
    response = client.post(f"/episodes/{episode_id}/review-chat", json={"text": CHOICE_B})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "choice-requires-multiple-drafts"


def test_alternatives_invalid_element_of_three_drops_individually(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alternatives keep per-element drops (an invalid alternative simply
    does not exist — journaled by warning), unlike command bundles."""

    _, second = _alternatives_set(
        client,
        workspace,
        monkeypatch,
        source_folder,
        [_proposal(12.0), _proposal(6.0), {"command_kind": "frobnicate"}],
    )
    assert len(_drafts_of(second)) == 2  # 1 invalid of 3 → the 2 valid serve
    assert [draft["target_seconds"] for draft in _drafts_of(second)] == [12.0, 6.0]


def test_alternatives_invalid_element_of_two_leaves_one(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, second = _alternatives_set(
        client,
        workspace,
        monkeypatch,
        source_folder,
        [{"command_kind": "frobnicate"}, _proposal(6.0)],
    )
    assert len(_drafts_of(second)) == 0  # single-draft sets ride in `draft`
    draft = cast("dict[str, object]", second["draft"])
    assert draft["target_seconds"] == 6.0  # 1 invalid of 2 → the valid one serves


def test_first_adoption_of_alternatives_consumes_the_set(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, second = _alternatives_set(
        client, workspace, monkeypatch, source_folder, [_proposal(12.0), _proposal(6.0)]
    )
    chosen = _apply(
        client,
        episode_id,
        {"text": BOTH_DIFFERENT, "drafts": [_drafts_of(second)[1]], "sequence": 2},
    )
    assert chosen.status_code == 200, chosen.text
    consumed = _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl")
    assert consumed[-1]["outcome"] == "chosen"
    other = _apply(
        client,
        episode_id,
        {"text": BOTH_DIFFERENT, "drafts": [_drafts_of(second)[0]], "sequence": 2},
    )
    assert other.status_code == 422
    assert other.json()["error"]["code"] == "proposal-consumed"  # the base moved with B
