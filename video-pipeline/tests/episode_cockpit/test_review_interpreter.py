"""Task 8: LLM review interpretation ahead of the deterministic validator.

The deterministic ``interpret_command`` is the fast path, the offline
fallback, and the regression oracle — the injected LLM is consulted only
for drafts it flagged ``needs_confirmation``, its proposal is confined to
the closed 12-kind set, and the deterministic confirmation rules are
re-applied verbatim (positional kinds never auto-confirm without an
explicit target). The LLM has no execution authority: the apply path is
untouched and re-interprets deterministically.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.review_chat import ReviewChatContext, interpret_command
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    build_nearby_context,
    build_review_llm_call,
    interpret,
)
from services.media_intelligence.models import (
    EditSourceSpan,
    MediaIntelligenceArtifact,
    MediaSource,
    Shot,
    ShotBestMoment,
    ShotConfidence,
    ShotCuttability,
    ShotEditorial,
    ShotVisual,
    TranscriptSegment,
)
from services.media_query.index_v2 import build_index

if TYPE_CHECKING:
    from collections.abc import Iterator

UNKNOWN_PHRASING = "ここもちょっと長ったらしくて省きたい"
PROPOSAL_KINDS = [
    "remove_section",
    "keep_longer",
    "use_other_take",
    "insert_broll",
    "mark_boring",
    "quiet_longer",
    "subtitle_shorter",
    "remove_effect",
    "lower_bgm",
    "match_color",
    "channel_lower_third",
    "episode_only",
]


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


def _proposal(
    kind: str | None = "remove_section",
    *,
    target: float | None = None,
    delta: float | None = None,
) -> dict:
    proposal: dict = {}
    if kind is not None:
        proposal["command_kind"] = kind
    if target is not None:
        proposal["target_seconds"] = target
    if delta is not None:
        proposal["seconds_delta"] = delta
    return proposal


def _fake_llm(
    proposal: dict | None,
) -> Callable[[str, ReviewChatContext, NearbyContext], dict]:
    def call(
        text: str, context: ReviewChatContext, nearby: NearbyContext
    ) -> dict:
        del text, context, nearby
        if proposal is None:
            raise TimeoutError("transport down")
        return proposal

    return call


# ---------------------------------------------------------------------------
# (a) fast path: confirmed deterministic drafts never touch the LLM
# ---------------------------------------------------------------------------


def test_known_command_with_llm_none_is_byte_equal_to_deterministic() -> None:
    text = "2:14の区間を削除"
    context = ReviewChatContext(at_seconds=None)
    draft = interpret(text, context, NearbyContext(), None)
    assert draft.model_dump() == interpret_command(text, context).model_dump()
    assert draft.command_kind == "remove_section"
    assert draft.target_seconds == 134.0
    assert draft.needs_confirmation is False


def test_confirmed_draft_skips_llm_even_when_injected() -> None:
    def exploding_llm(
        text: str, context: ReviewChatContext, nearby: NearbyContext
    ) -> dict:
        del text, context, nearby
        raise AssertionError("fast path must never call the LLM")

    draft = interpret(
        "2:14の区間を削除", ReviewChatContext(at_seconds=None), NearbyContext(), exploding_llm
    )
    assert draft.needs_confirmation is False


def test_llm_none_unknown_phrasing_is_exactly_today_behavior() -> None:
    context = ReviewChatContext(at_seconds=12.5)
    draft = interpret(UNKNOWN_PHRASING, context, NearbyContext(at_seconds=12.5), None)
    assert draft.model_dump() == interpret_command(UNKNOWN_PHRASING, context).model_dump()
    assert draft.needs_confirmation is True


# ---------------------------------------------------------------------------
# (b) LLM proposals re-enter through the draft validator + confirmation rules
# ---------------------------------------------------------------------------


def test_positional_llm_proposal_without_target_never_auto_confirms() -> None:
    draft = interpret(
        UNKNOWN_PHRASING,
        ReviewChatContext(at_seconds=None),
        NearbyContext(),
        _fake_llm(_proposal(target=None)),
    )
    assert draft.command_kind == "remove_section"
    assert draft.target_seconds is None
    assert draft.needs_confirmation is True
    assert draft.confirmation_reason


def test_llm_proposal_with_explicit_target_clear_drafts() -> None:
    draft = interpret(
        UNKNOWN_PHRASING,
        ReviewChatContext(at_seconds=None),
        NearbyContext(),
        _fake_llm(_proposal(target=134.0)),
    )
    assert draft.command_kind == "remove_section"
    assert draft.target_seconds == 134.0
    assert draft.needs_confirmation is False
    assert draft.confirmation_reason is None


def test_llm_kind_outside_closed_set_falls_back_to_deterministic_flagged_draft() -> None:
    context = ReviewChatContext(at_seconds=10.0)
    draft = interpret(
        UNKNOWN_PHRASING, context, NearbyContext(), _fake_llm(_proposal("nuke_timeline"))
    )
    deterministic = interpret_command(UNKNOWN_PHRASING, context)
    assert draft.model_dump() == deterministic.model_dump()
    assert draft.command_kind is None
    assert draft.confirmation_reason == deterministic.confirmation_reason


def test_llm_transport_error_returns_deterministic_flagged_draft() -> None:
    context = ReviewChatContext(at_seconds=10.0)
    draft = interpret(
        UNKNOWN_PHRASING, context, NearbyContext(), _fake_llm(None)
    )
    deterministic = interpret_command(UNKNOWN_PHRASING, context)
    assert draft.model_dump() == deterministic.model_dump()


def test_non_positional_llm_proposal_clear_without_target() -> None:
    draft = interpret(
        UNKNOWN_PHRASING,
        ReviewChatContext(at_seconds=None),
        NearbyContext(),
        _fake_llm(_proposal("subtitle_shorter")),
    )
    assert draft.command_kind == "subtitle_shorter"
    assert draft.needs_confirmation is False


def test_llm_delta_only_applies_to_keep_longer_rule() -> None:
    draft = interpret(
        UNKNOWN_PHRASING,
        ReviewChatContext(at_seconds=None),
        NearbyContext(),
        _fake_llm(_proposal("remove_section", target=134.0, delta=9.0)),
    )
    assert draft.seconds_delta is None  # deterministic rule: delta is keep_longer-only


# ---------------------------------------------------------------------------
# (c) tolerant factory: regex-only unless mode + env gate + pin + transport
# ---------------------------------------------------------------------------

_PROD_ENV = {"EDITORIAL_DIRECTOR_API_KEY": "test-key", "EDITORIAL_DIRECTOR_NETWORK_ENABLED": "1"}


def _write_runtime(tmp_path: Path, mode: str) -> Path:
    path = tmp_path / "editorial-runtime.json"
    path.write_text(json.dumps({"schema_version": "editorial-runtime-v1", "mode": mode}))
    return path


def _write_pin(tmp_path: Path) -> Path:
    path = tmp_path / "review-interpreter.json"
    path.write_text(json.dumps({"model_id": "gpt-5.6-sol", "endpoint": "https://api.openai.com/v1/responses"}))
    return path


def test_factory_without_runtime_config_returns_none(tmp_path: Path) -> None:
    assert build_review_llm_call(
        runtime_path=tmp_path / "missing.json", pin_path=_write_pin(tmp_path), env=_PROD_ENV
    ) is None


def test_factory_heuristic_mode_returns_none(tmp_path: Path) -> None:
    assert build_review_llm_call(
        runtime_path=_write_runtime(tmp_path, "heuristic_diagnostic"),
        pin_path=_write_pin(tmp_path),
        env=_PROD_ENV,
    ) is None


def test_factory_production_mode_without_env_returns_none(tmp_path: Path) -> None:
    assert (
        build_review_llm_call(
            runtime_path=_write_runtime(tmp_path, "production_model"),
            pin_path=_write_pin(tmp_path),
            env={"EDITORIAL_DIRECTOR_API_KEY": "", "EDITORIAL_DIRECTOR_NETWORK_ENABLED": ""},
        )
        is None
    )


def test_factory_production_mode_without_pin_returns_none(tmp_path: Path) -> None:
    assert (
        build_review_llm_call(
            runtime_path=_write_runtime(tmp_path, "production_model"),
            pin_path=tmp_path / "absent-pin.json",
            env=_PROD_ENV,
        )
        is None
    )


def test_factory_builds_call_with_injected_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def make_http_post(env=None):  # fake of the task-3 signature
        del env

        def http_post(*, url: str, headers: dict[str, str], body: bytes, timeout_s: float) -> bytes:
            captured.update(
                url=url, body=body.decode("utf-8"), timeout_s=timeout_s,
                content_type=headers["Content-Type"],
            )
            return json.dumps(
                {"output_text": json.dumps(_proposal(target=134.0))}
            ).encode("utf-8")

        return http_post

    fake_module = types.ModuleType("services.cli.live_editorial_v2")
    fake_module.make_http_post = make_http_post  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.cli.live_editorial_v2", fake_module)

    llm = build_review_llm_call(
        runtime_path=_write_runtime(tmp_path, "production_model"),
        pin_path=_write_pin(tmp_path),
        env=_PROD_ENV,
    )
    assert llm is not None
    proposal = llm(UNKNOWN_PHRASING, ReviewChatContext(at_seconds=None), NearbyContext())
    assert proposal["command_kind"] == "remove_section"

    request = json.loads(str(captured["body"]))
    assert request["model"] == "gpt-5.6-sol"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    schema = request["text"]["format"]["schema"]
    kind_enum = schema["$defs"]["ReviewCommandKind"]["enum"]
    assert sorted(kind_enum) == sorted(PROPOSAL_KINDS)
    data = json.loads(request["input"][1]["content"])
    assert data["operator_message"] == UNKNOWN_PHRASING
    assert request["input"][1]["role"] == "user"


def test_factory_transport_construction_failure_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make_http_post(env=None):  # fake refusing construction
        del env
        raise RuntimeError("gate refused")

    fake_module = types.ModuleType("services.cli.live_editorial_v2")
    fake_module.make_http_post = make_http_post  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.cli.live_editorial_v2", fake_module)
    assert (
        build_review_llm_call(
            runtime_path=_write_runtime(tmp_path, "production_model"),
            pin_path=_write_pin(tmp_path),
            env=_PROD_ENV,
        )
        is None
    )


# ---------------------------------------------------------------------------
# (d) nearby context: tolerant reads over the episode's v2 media index
# ---------------------------------------------------------------------------


def test_nearby_context_without_index_is_position_only(tmp_path: Path) -> None:
    nearby = build_nearby_context(tmp_path, at_seconds=12.5)
    assert nearby == NearbyContext(at_seconds=12.5)


def test_nearby_context_without_position_is_empty(tmp_path: Path) -> None:
    assert build_nearby_context(tmp_path, at_seconds=None) == NearbyContext(at_seconds=None)


def test_nearby_context_degrades_on_non_index_file(tmp_path: Path) -> None:
    (tmp_path / "media-intelligence.duckdb").write_bytes(b"not-a-duckdb-file")
    assert build_nearby_context(tmp_path, at_seconds=12.5) == NearbyContext(at_seconds=12.5)


def test_nearby_context_reads_shots_and_transcript(tmp_path: Path) -> None:
    artifact = MediaIntelligenceArtifact(
        episode_id="ep-nearby",
        sources=(MediaSource(source_id="src-cam-a", duration_frames=300),),
        shots=(
            Shot(
                shot_id="shot-a",
                source_span=EditSourceSpan(start_frame=0, end_frame=150),
                description="studio close-up demo",
                visual=ShotVisual(shot_size="close_up", camera_motion="static"),
                editorial=ShotEditorial(
                    role="talking_head",
                    select_potential="high",
                    best_moment=ShotBestMoment(frame=75, why="決定瞬間"),
                    pacing="moderate",
                    cuttability=ShotCuttability.model_validate({"in": "clean", "out": "clean"}),
                ),
                transcript_segments=(
                    TranscriptSegment(
                        segment_id="tr-a1", text="この製品は軽量です", start_frame=10, end_frame=90
                    ),
                ),
                confidence=ShotConfidence(editorial="medium", visual="high"),
            ),
        ),
    )
    episode_dir = tmp_path / "ep-nearby"
    episode_dir.mkdir()
    build_index(artifact, episode_dir / "media-intelligence.duckdb")

    nearby = build_nearby_context(episode_dir, at_seconds=5.0)
    assert nearby.at_seconds == 5.0
    assert nearby.shot_description == "studio close-up demo"
    assert nearby.transcript_snippet == "この製品は軽量です"


# ---------------------------------------------------------------------------
# (e) route wiring: /review-chat returns the interpreted draft, shape intact
# ---------------------------------------------------------------------------


def test_review_chat_route_with_fake_llm_returns_clear_draft(
    client: TestClient, source_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call",
        lambda: _fake_llm(_proposal(target=134.0)),
    )
    episode_id = _create_episode(client, source_folder)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": UNKNOWN_PHRASING, "at_seconds": 10.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"received", "sequence", "draft"}
    draft = body["draft"]
    assert draft["command_kind"] == "remove_section"
    assert draft["target_seconds"] == 134.0
    assert draft["needs_confirmation"] is False


def test_review_chat_route_regex_only_when_env_gate_closed(
    client: TestClient, source_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EDITORIAL_DIRECTOR_API_KEY", raising=False)
    monkeypatch.delenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", raising=False)
    episode_id = _create_episode(client, source_folder)
    response = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": UNKNOWN_PHRASING}
    )
    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["command_kind"] is None
    assert draft["needs_confirmation"] is True
    assert draft["confirmation_reason"]
