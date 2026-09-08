"""UX phase 2.5 slice-1: consultation routes (failure-first).

Routes live in an additive router (``api_consultation.py``) registered
alongside ``api.router``: GET lists the consultations journal, POST
``/consultation/message`` creates one consultation with its LLM proposal
set (typed 422 when the budget is exhausted BEFORE any LLM call, and a
typed honest error in diagnostic mode — no heuristic fallback), and POST
``/consultation/judgment`` appends an append-only judgment.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit import api_consultation, consultation_store
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.consultation_store import ConsultationBudgetLimits

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_DETAILS = {
    "audience_message": "始めて見る人に分かる導入",
    "structure": "導入→本編→締め",
    "duration_estimate": "約4分",
    "candidate_scenes": ["opening", "demo"],
    "subtitle_policy": "短めの字幕",
    "audio_policy": "BGM小さめ",
    "tempo_policy": "前半テンポ重視",
    "reference_mapping": "参考1の構成を踏襲",
    "unused_reasons": "未使用素材はなし",
    "unconfirmed": ["尺の希望"],
}


def _envelope(count: int) -> dict[str, object]:
    return {
        "proposals": [
            {
                "title": f"案{i}",
                "summary": f"要旨{i}",
                "details": dict(_DETAILS),
            }
            for i in range(1, count + 1)
        ]
    }


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


@pytest.fixture
def llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Hermetic fake of the production-model consultation factory.

    The default fake answers ONE proposal per message and records every
    message that reached the model. Individual tests re-stub the factory
    (None / raising / multi-proposal envelopes) on top of this.
    """

    calls: list[str] = []

    def factory() -> Callable[[str], dict[str, object]]:
        def call(message: str) -> dict[str, object]:
            calls.append(message)
            return _envelope(1)

        return call

    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", factory)
    return calls


def _set_limits(monkeypatch: pytest.MonkeyPatch, **overrides: float) -> None:
    limits = ConsultationBudgetLimits(
        llm_calls_limit=int(overrides.get("llm_calls_limit", 6)),
        intervals_limit=int(overrides.get("intervals_limit", 3)),
        wall_seconds_limit=float(overrides.get("wall_seconds_limit", 600.0)),
    )
    monkeypatch.setattr(api_consultation, "load_budget_limits", lambda: limits)


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "相談フェーズ用"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _post_message(
    client: TestClient, episode_id: str, message: str = "短くしたい"
) -> dict[str, Any]:
    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": message}
    )
    assert response.status_code == 200
    return dict(response.json())


# ---------------------------------------------------------------------------
# GET /episodes/{id}/consultation: the journal view
# ---------------------------------------------------------------------------


def test_unknown_episode_is_typed_404(client: TestClient) -> None:
    response = client.get("/episodes/does-not-exist/consultation")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "episode-not-found"


def test_get_lists_consultations_newest_journal_order(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)

    empty = client.get(f"/episodes/{episode_id}/consultation")
    assert empty.status_code == 200
    assert empty.json() == {"consultations": []}

    created = _post_message(client, episode_id, "まず一回目")
    listed = client.get(f"/episodes/{episode_id}/consultation")

    assert listed.status_code == 200
    body = listed.json()
    assert [c["consultation_id"] for c in body["consultations"]] == [
        created["consultation_id"]
    ]
    assert body["consultations"][0]["message"] == "まず一回目"


# ---------------------------------------------------------------------------
# POST /episodes/{id}/consultation/message: proposal generation
# ---------------------------------------------------------------------------


def test_message_creates_consultation_with_one_proposal(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)

    body = _post_message(client, episode_id, "ブログ告知用に短くしたい")

    assert set(body) == {
        "consultation_id", "created_at", "message", "proposals", "judgments", "budget"
    }
    assert body["message"] == "ブログ告知用に短くしたい"
    assert body["judgments"] == []
    assert len(body["proposals"]) == 1
    assert set(body["proposals"][0]) == {"proposal_id", "title", "summary", "details"}
    assert body["proposals"][0]["details"] == _DETAILS
    assert llm_calls == ["ブログ告知用に短くしたい"]
    assert body["budget"] == {
        "llm_calls_used": 1, "llm_calls_limit": 6,
        "intervals_used": 1, "intervals_limit": 3,
        "wall_seconds_limit": 600.0,
        "cost_display": "unmeasured",
        "wall_seconds_used": body["budget"]["wall_seconds_used"],
    }
    assert body["budget"]["wall_seconds_used"] >= 0.0


def test_budget_never_resets_across_regenerations(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)

    first = _post_message(client, episode_id, "一回目")
    second = _post_message(client, episode_id, "もう一度やり直し")

    assert first["budget"]["llm_calls_used"] == 1
    assert second["budget"]["llm_calls_used"] == 2
    assert second["budget"]["intervals_used"] == 2
    assert second["budget"]["wall_seconds_used"] >= first["budget"]["wall_seconds_used"]


def test_exhausted_budget_returns_typed_422_before_any_llm_call(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_limits(monkeypatch, llm_calls_limit=1)
    episode_id = _create_episode(client, source_folder)
    _post_message(client, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": "三回目"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-budget-exhausted"
    assert len(llm_calls) == 1  # the exhausted request never reached the model


def test_exhausted_wall_limit_returns_typed_422_before_any_llm_call(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_limits(monkeypatch, wall_seconds_limit=0.0)
    episode_id = _create_episode(client, source_folder)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": "一回目"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-budget-exhausted"
    assert llm_calls == []


def test_diagnostic_mode_returns_typed_llm_unavailable_without_fabricating(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", lambda: None)
    episode_id = _create_episode(client, source_folder)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": "短くしたい"}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "consultation-llm-unavailable"
    assert "production model" in error["detail"]
    assert llm_calls == []
    listed = client.get(f"/episodes/{episode_id}/consultation")
    assert listed.json() == {"consultations": []}


def test_llm_failure_is_typed_422_and_persists_nothing(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory() -> Callable[[str], dict[str, object]]:
        def call(_message: str) -> dict[str, object]:
            raise RuntimeError("transport down")

        return call

    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", factory)
    episode_id = _create_episode(client, source_folder)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": "短くしたい"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-llm-failed"
    listed = client.get(f"/episodes/{episode_id}/consultation")
    assert listed.json() == {"consultations": []}


def test_two_proposals_only_when_direction_genuinely_splits(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory() -> Callable[[str], dict[str, object]]:
        def call(message: str) -> dict[str, object]:
            llm_calls.append(message)
            return _envelope(2)

        return call

    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", factory)
    episode_id = _create_episode(client, source_folder)

    body = _post_message(client, episode_id)

    assert len(body["proposals"]) == 2
    assert [p["proposal_id"] for p in body["proposals"]] == ["prop-1", "prop-2"]


def test_proposals_are_capped_at_two(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory() -> Callable[[str], dict[str, object]]:
        def call(message: str) -> dict[str, object]:
            llm_calls.append(message)
            return _envelope(3)

        return call

    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", factory)
    episode_id = _create_episode(client, source_folder)

    body = _post_message(client, episode_id)

    assert len(body["proposals"]) == 2


def test_malformed_llm_response_is_typed_422(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory() -> Callable[[str], dict[str, object]]:
        def call(_message: str) -> dict[str, object]:
            return {"proposals": "not-a-list"}

        return call

    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", factory)
    episode_id = _create_episode(client, source_folder)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": "短くしたい"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-llm-failed"


# ---------------------------------------------------------------------------
# POST /episodes/{id}/consultation/judgment: adoption records
# ---------------------------------------------------------------------------


def test_judgment_is_append_only_and_scope_verbatim(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)
    consultation_id = str(_post_message(client, episode_id)["consultation_id"])

    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": True, "appearance": False, "audio": False},
            "note": "構成だけ採用、見た目未確認",
        },
    )
    second = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": None,
            "decision": "both_wrong",
            "scope": {"composition": False, "appearance": True, "audio": True},
            "note": None,
        },
    )

    assert first.status_code == 200
    judgments = first.json()["judgments"]
    assert len(judgments) == 1
    assert judgments[0]["proposal_id"] == "prop-1"
    assert judgments[0]["decision"] == "adopt"
    assert judgments[0]["scope"] == {
        "composition": True, "appearance": False, "audio": False
    }
    assert judgments[0]["note"] == "構成だけ採用、見た目未確認"
    assert second.status_code == 200
    judgments_after = second.json()["judgments"]
    assert len(judgments_after) == 2  # append-only: the first judgment stands
    assert judgments_after[0] == judgments[0]
    assert judgments_after[1]["decision"] == "both_wrong"
    assert judgments_after[1]["proposal_id"] is None


def test_judgment_empty_decision_is_typed_4xx(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)
    consultation_id = str(_post_message(client, episode_id)["consultation_id"])

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": None,
            "decision": "",
            "scope": {"composition": False, "appearance": False, "audio": False},
            "note": None,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation-error"
    listed = client.get(f"/episodes/{episode_id}/consultation")
    assert listed.json()["consultations"][0]["judgments"] == []


def test_judgment_unknown_consultation_is_typed_404(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": "missing",
            "proposal_id": None,
            "decision": "adopt",
            "scope": {"composition": True, "appearance": False, "audio": False},
            "note": None,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "consultation-not-found"


def test_judgment_unknown_proposal_is_typed_422(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    episode_id = _create_episode(client, source_folder)
    consultation_id = str(_post_message(client, episode_id)["consultation_id"])

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-9",
            "decision": "reject",
            "scope": {"composition": False, "appearance": False, "audio": False},
            "note": None,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-proposal-not-found"


# ---------------------------------------------------------------------------
# generation stays OFF: no image-generation machinery is ever imported
# ---------------------------------------------------------------------------


def test_generation_stays_off(
    client: TestClient, source_folder: Path, llm_calls: list[str]
) -> None:
    """The consultation modules' own import graph must stay generation-free.

    Checked against the module SOURCES (process ``sys.modules`` is polluted
    by whichever other test module imported a generation module first).
    Exercise happens with the factory active so any lazy import would run.
    """

    episode_id = _create_episode(client, source_folder)
    _post_message(client, episode_id)

    module_paths = [
        Path(api_consultation.__file__ or ""),
        Path(consultation_store.__file__ or ""),
    ]
    assert module_paths
    for module_path in module_paths:
        imports = [
            line.strip()
            for line in module_path.read_text().splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "generation" in line]
        assert not [line for line in imports if "edit_plan_generate" in line]
        assert not [line for line in imports if "image" in line]
