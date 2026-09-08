"""UX redesign 工程2: reactions to proposal sets (U02-U05, brief §3.3/§6.2).

「両方違う」 (explicit rejection of BOTH drafts → re-investigation into an
ALTERNATIVES set, never a silent repeat), 「前よりいい。でもせわしない」
(continuation adjusting the prior proposal in context — still a command
BUNDLE) — classified AFTER direct commands and BEFORE feelings. The
choice-reaction contract lives in test_review_proposal_kinds.py (choices
are valid only against alternatives sets). No preference/taste/reference-
library write happens anywhere (工程3 out of scope); whole-video feelings
never require seconds (U02).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.review_chat import ReviewChatContext, ReviewStoreLocation
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    ReviewLlmProposals,
)
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

WHOLE_VIDEO = "全体が素人っぽい"
BOTH_DIFFERENT = "両方違う"
CONTINUATION = "前よりいい。でもせわしない"
CHOICE_B = "Bが好き"
P1 = [{"command_kind": "remove_section", "target_seconds": 12.0,
       "hypothesis_ja": "同じ説明が続くのが原因の可能性の仮説"}]
P2 = [{"command_kind": "remove_section", "target_seconds": 6.0,
       "hypothesis_ja": "間の余白が足りない可能性の仮説"}]


def _llm(*returns: list[dict], capture: list | None = None) -> object:
    state = {"n": 0}

    def call(text: str, context: ReviewChatContext, nearby: NearbyContext) -> dict:
        del text, nearby
        if capture is not None:
            capture.append(context)
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


def _jsonl(workspace: dict[str, Path], episode_id: str, name: str) -> list[dict[str, object]]:
    log = workspace["episodes_root"] / episode_id / name
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_bytes().splitlines()]


def _draft_of(body: dict[str, object]) -> dict[str, object]:
    """The primary draft of a chat response, typed."""

    return cast("dict[str, object]", body["draft"])


def _drafts_of(body: dict[str, object]) -> list[dict[str, object]]:
    """The (possibly absent) multi-draft list of a chat response, typed."""

    return cast("list[dict[str, object]]", body.get("drafts", []))


def test_u02_whole_video_feeling_needs_no_seconds_shows_checked_materials(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: _llm(P1))
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, WHOLE_VIDEO)  # no at_seconds anywhere
    assert _draft_of(body)["needs_confirmation"] is True
    assert "timestamp" not in str(_draft_of(body)["confirmation_reason"])
    assert _draft_of(body)["investigated"] is True
    assert _draft_of(body)["hypothesis"] == P1[0]["hypothesis_ja"]  # separate field
    assert body["investigation_state"] == "hypothesis-proposed"
    assert body["checked_materials"] == {"transcript": False, "shot": False}


def test_u02_reprototype_via_both_different_links_new_set(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _llm(P1, P2)  # ONE closure: call state survives across chats
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _chat(client, episode_id, WHOLE_VIDEO)
    body = _chat(client, episode_id, BOTH_DIFFERENT)
    assert body["reaction"] == "both-different"
    assert body["in_response_to_set"] == 1
    assert body["responds_to_set"] == 1
    assert body["proposal_kind"] == "alternatives"  # both-different → alternatives
    sets = _jsonl(workspace, episode_id, "review-proposals.jsonl")
    assert len(sets) == 2
    assert sets[1]["responds_to_set"] == 1
    assert sets[1]["reaction_kind"] == "both-different"
    assert sets[1]["proposal_kind"] == "alternatives"
    assert sets[1]["drafts"] != sets[0]["drafts"]  # no silent repeat
    consumed = _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl")
    assert [entry["set_sequence"] for entry in consumed] == [1]
    assert consumed[0]["outcome"] == "rejected"
    stale = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": WHOLE_VIDEO, "sequence": 1, "drafts": sets[0]["drafts"]},
    )
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "proposal-consumed"


def test_u05_identical_reproposal_flagged_not_repeated_no_preference_writes(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: _llm(P1))
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _chat(client, episode_id, WHOLE_VIDEO)
    body = _chat(client, episode_id, BOTH_DIFFERENT)  # LLM repeats P1 verbatim
    assert _draft_of(body)["command_kind"] is None  # honest: no new proposal
    assert _draft_of(body)["needs_confirmation"] is True
    assert "両方とも違う" in str(_draft_of(body)["confirmation_reason"])
    sets = _jsonl(workspace, episode_id, "review-proposals.jsonl")
    assert sets[1]["responds_to_set"] == 1
    assert sets[1]["reaction_kind"] == "both-different"
    saved_drafts = cast("list[dict[str, object]]", sets[1]["drafts"])
    kinds = [draft.get("command_kind") for draft in saved_drafts]
    assert "remove_section" not in kinds  # no silent repeat
    consumed = _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl")
    assert consumed[0]["outcome"] == "rejected"
    names = [p.name for p in (workspace["episodes_root"] / episode_id).rglob("*")]
    assert not any("taste" in n or "reference-library" in n for n in names)


def test_continuation_feeds_prior_context_and_records_linkage(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: list[ReviewChatContext] = []
    fake = _llm(P1, P2, capture=capture)  # ONE closure across chats
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _chat(client, episode_id, WHOLE_VIDEO)
    body = _chat(client, episode_id, CONTINUATION)
    assert body["reaction"] == "continuation"
    assert capture[1].reaction_kind == "continuation"
    assert capture[1].prior_set is not None
    assert capture[1].prior_set.set_sequence == 1
    assert capture[1].prior_set.drafts[0].command_kind == "remove_section"
    assert _draft_of(body)["command_kind"] == "remove_section"  # adjusted...
    assert _draft_of(body)["target_seconds"] == 6.0  # ...not restated
    assert body["proposal_kind"] == "command-bundle"  # continuation stays a bundle
    sets = _jsonl(workspace, episode_id, "review-proposals.jsonl")
    assert sets[1]["responds_to_set"] == 1
    assert sets[1]["reaction_kind"] == "continuation"
    assert sets[1]["proposal_kind"] == "command-bundle"
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []
    entry = _jsonl(workspace, episode_id, "review-chat.jsonl")[1]
    assert entry["in_response_to_set"] == 1


@pytest.mark.parametrize("text", [BOTH_DIFFERENT, CHOICE_B, CONTINUATION])
def test_reaction_without_any_proposal_set_is_typed_error(
    client: TestClient, workspace: dict[str, Path], source_folder: Path, text: str
) -> None:
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    response = client.post(f"/episodes/{episode_id}/review-chat", json={"text": text})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no-proposal-to-react-to"


def test_bundle_route_enumerates_all_fixes_and_schema_is_uncapped(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    three = P1 + P2 + [{"command_kind": "remove_section", "target_seconds": 3.0}]
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: _llm(three))
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, WHOLE_VIDEO)
    assert len(_drafts_of(body)) == 3  # 明示的な複数修正はすべて列挙 — no 間引き
    assert body["proposal_kind"] == "command-bundle"
    bounds = ReviewLlmProposals.model_json_schema()["properties"]["proposals"]
    assert bounds["minItems"] == 1
    assert "maxItems" not in bounds  # uncapped: the max-2 rule is alternatives-only

