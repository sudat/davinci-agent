"""UX redesign 工程1: feelings routing end-to-end (U01, brief §5.3).

「ここ退屈」 with a player position must NOT auto-confirm — it records an
investigation attempt (materials gathered, never a cause-identified
claim), and when a (fake) LLM is available its proposal carries a
testable hypothesis. A feeling riding an explicit command
(「退屈なところを削除して」) is a DIRECT command: no investigation marking.
The display contract rides ``investigation_state``:
hypothesis-proposed / materials-checked / unconfirmed.
"""

# allow: SIZE_OK — one feelings-routing concern per file (deterministic vs
# LLM proposals vs display contract); splitting scatters one U01 story.

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.episode_files import FileOps
from services.episode_cockpit.review_chat import ReviewChatContext, ReviewStoreLocation
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    _request_parts,
)
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

FEELINGS_TEXT = "ここ退屈"
FEELINGS_AT = 6.0
REMOVE_TEXT = "この区間を削除して"
FEELING_COMMAND_TEXT = "退屈なところを削除して"
MOVIE_TEXT = "映画っぽく"


def _fake_llm(hypothesis: str, target: float) -> object:
    def call(text: str, context: ReviewChatContext, nearby: NearbyContext) -> dict:
        del text, context, nearby
        return {
            "proposals": [
                {
                    "command_kind": "remove_section",
                    "target_seconds": target,
                    "hypothesis_ja": hypothesis,
                }
            ]
        }

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


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "travel vlog"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_store(workspace: dict[str, Path], episode_id: str) -> None:
    store = ReviewStoreLocation(
        log_path=workspace["episodes_root"] / episode_id / "review" / "events.jsonl",
        plan_dir=workspace["episodes_root"] / episode_id / "review" / "store",
    )
    initialize_store(manifest_plan("p0c-remove-clear"), store.log_path, store.plan_dir)


def _chat_lines(workspace: dict[str, Path], episode_id: str) -> list[dict[str, object]]:
    log = workspace["episodes_root"] / episode_id / "review-chat.jsonl"
    return [json.loads(line) for line in log.read_bytes().splitlines()]


def test_feelings_with_position_records_investigation_and_stays_flagged(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["needs_confirmation"] is True  # U01: NO auto-confirm
    assert body["draft"]["command_kind"] == "mark_boring"
    assert body["draft"]["investigated"] is True
    assert body["draft"]["hypothesis"] is None
    assert body["investigated"] is True
    assert body["hypothesis"] is None
    assert body["investigation_state"] == "unconfirmed"
    entry = _chat_lines(workspace, episode_id)[0]
    assert entry["investigated"] is True
    assert entry["hypothesis"] is None


def test_feelings_with_fake_llm_carries_hypothesis(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hypothesis = "同じ説明が続いているのが原因の可能性"
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call",
        lambda: _fake_llm(hypothesis, FEELINGS_AT),
    )
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["needs_confirmation"] is True  # U01: NO auto-confirm
    assert body["draft"]["hypothesis"] == hypothesis
    assert body["draft"]["investigated"] is True
    assert body["hypothesis"] == hypothesis
    assert body["investigation_state"] == "hypothesis-proposed"
    entry = _chat_lines(workspace, episode_id)[0]
    assert entry["investigated"] is True
    assert entry["hypothesis"] == hypothesis


def test_feelings_with_materials_reports_materials_checked(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_nearby(
        self: object, episode_id: str, *, at_seconds: float | None
    ) -> NearbyContext:
        del self, episode_id
        return NearbyContext(at_seconds=at_seconds, transcript_snippet="確認した発話")

    monkeypatch.setattr(FileOps, "nearby_context", fake_nearby)
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["hypothesis"] is None
    assert body["investigation_state"] == "materials-checked"


def test_direct_command_with_feeling_word_is_not_investigated(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELING_COMMAND_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["command_kind"] == "remove_section"
    assert body["draft"]["needs_confirmation"] is False
    assert body["draft"]["investigated"] is False
    assert "investigation_state" not in body
    assert _chat_lines(workspace, episode_id)[0]["investigated"] is False


def test_direct_command_response_keeps_old_shape(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": REMOVE_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"received", "sequence", "draft", "proposal_kind"}
    assert body["proposal_kind"] == "command-bundle"
    assert body["draft"]["needs_confirmation"] is False


def test_u03_movie_feeling_never_maps_to_fixed_transform(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    """U03 (工程2): 「映画っぽく」 investigates; no fixed darken/letterbox mapping,
    and the served LLM instructions carry the no-fixed-mapping rule."""

    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": MOVIE_TEXT}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["command_kind"] is None  # NO fixed-transform command
    assert body["draft"]["needs_confirmation"] is True
    assert body["investigated"] is True
    assert body["investigation_state"] == "unconfirmed"
    assert body["checked_materials"] == {"transcript": False, "shot": False}
    system_text, _ = _request_parts(MOVIE_TEXT, ReviewChatContext(), NearbyContext())
    assert "映画風" in system_text
    assert "暗く" in system_text
    assert "黒帯" in system_text


def test_checked_materials_reflect_what_was_checked(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工程2 U02: checked_materials mirrors ONLY what was gathered (transcript/
    scene text; video/audio are never checked, never claimed)."""

    def fake_nearby(
        self: object, episode_id: str, *, at_seconds: float | None
    ) -> NearbyContext:
        del self, episode_id, at_seconds
        return NearbyContext(at_seconds=None, transcript_snippet="確認した発話")

    monkeypatch.setattr(FileOps, "nearby_context", fake_nearby)
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": MOVIE_TEXT}
    )
    assert response.status_code == 200
    assert response.json()["checked_materials"] == {"transcript": True, "shot": False}


def test_old_review_chat_line_still_counts_sequence(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    log = workspace["episodes_root"] / episode_id / "review-chat.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    old_entry = {
        "schema_version": "cockpit-review-chat-v1",
        "sequence": 1,
        "text": "古い入力",
        "at_seconds": None,
    }
    log.write_text(json.dumps(old_entry, ensure_ascii=False) + "\n", encoding="utf-8")
    response = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": "新しい入力です"}
    )
    assert response.status_code == 200
    assert response.json()["sequence"] == 2


def test_confirmed_feelings_proposal_applies_and_rebuild_plans(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call",
        lambda: _fake_llm("仮説", FEELINGS_AT),
    )
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    ).json()
    draft = preview["draft"]
    apply_response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={
            "text": FEELINGS_TEXT,
            "at_seconds": FEELINGS_AT,
            "drafts": [draft],
        },
    )
    assert apply_response.status_code == 200, apply_response.text
    applied = apply_response.json()["applied"]
    assert applied["result_plan_version"] == "v2"
    assert applied["command_kind"] == "remove_section"
