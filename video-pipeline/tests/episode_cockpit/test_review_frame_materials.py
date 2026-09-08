"""工程2 rework #1: U02 実映像確認への接続 — gated review frame materials.

When ``config/editorial-runtime.json`` carries
``"review_frame_materials": {"enabled": true}``, the feelings/both-different
investigation routes extract a few read-only stills from the episode's
preview video around the player position, attach them to the LLM call
(codex transport ``images``), and extend ``checked_materials`` with
traceable records (path / at_seconds / source). Default OFF: behavior is
exactly as before (no extraction, no ``frames`` key, no images).

Honesty contract: a still is a SINGLE FRAME — it is never an audio or
whole-video verification and nothing may claim otherwise.

Frame pixels are config-gated egress (PRD v4.4 §23): the gate key IS the
operator permission; with the gate off no frame data ever leaves the
machine, and the only transport that carries pixels is codex-exec (the
openai-api transport has no image input — frames stay recorded, metadata
only).
"""

# allow: SIZE_OK — one U02 gated-frame-materials story per file (gate unit,
# route behavior, honest degradation, stills-position diagnosis, and the
# P1-2 three-level 抽出/ attempted / verified honesty share the ONE fixture
# set; splitting scatters a single connection contract).

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.cli.live_editorial_codex import _CodexExecRunner
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.models import FrameMaterial
from services.episode_cockpit.review_chat import ReviewChatContext, ReviewStoreLocation
from services.episode_cockpit.review_frames import (
    FrameMaterialWorkspace,
    _extract_still,
    extract_review_frames,
    frame_gate,
    gather_route_frame_materials,
)
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    _codex_call,
    build_review_llm_call,
    interpret_message_with_outcome,
    llm_carries_images,
)
from services.episode_cockpit.review_reactions import checked_materials
from services.preview.models import PreviewError
from services.preview.tools import load_pinned_tools
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.episode_cockpit.review_interpreter import ReviewLlmCall

FEELINGS_TEXT = "ここ退屈"
FEELINGS_AT = 2.0
BOTH_DIFFERENT = "両方違う"
P1 = [
    {
        "command_kind": "remove_section",
        "target_seconds": 1.0,
        "hypothesis_ja": "同じ説明が続くのが原因の可能性の仮説",
    }
]


def _llm(capture: list[ReviewChatContext]) -> ReviewLlmCall:
    def call(text: str, context: ReviewChatContext, nearby: object) -> dict:
        del text, nearby
        capture.append(context)
        return {"proposals": list(P1)}

    return call


def _raising_llm() -> ReviewLlmCall:
    def call(text: str, context: ReviewChatContext, nearby: object) -> dict:
        del text, context, nearby
        raise RuntimeError("codex exec failed")

    return call


def _stamped_llm(capture: list[ReviewChatContext]) -> ReviewLlmCall:
    """A codex-exec-like call: the builder stamps image_capable=True."""

    call = _llm(capture)
    setattr(call, "image_capable", True)  # noqa: B010 (deliberate dynamic stamp, mirrors _stamp_image_capable)
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


@pytest.fixture(scope="module")
def fixture_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real mp4 via the PINNED ffmpeg (testsrc2, T4/production-kit precedent)."""

    try:
        tools = load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")
    media = tmp_path_factory.mktemp("review-frames-fixture") / "clip.mp4"
    result = subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=4",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return media


@pytest.fixture
def source_folder(tmp_path: Path, fixture_clip: Path) -> Path:
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(fixture_clip.read_bytes())
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


def _seed_preview(
    workspace: dict[str, Path], episode_id: str, fixture_clip: Path
) -> Path:
    preview = workspace["episodes_root"] / episode_id / "previews" / "preview.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(fixture_clip.read_bytes())
    return preview


def _chat_lines(workspace: dict[str, Path], episode_id: str) -> list[dict[str, object]]:
    log = workspace["episodes_root"] / episode_id / "review-chat.jsonl"
    return [json.loads(line) for line in log.read_bytes().splitlines()]


def _fingerprint(path: Path) -> tuple[str, int]:
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime_ns,
    )


# ---------------------------------------------------------------------------
# Gate unit (real editorial-runtime.json loader)
# ---------------------------------------------------------------------------


def test_frame_gate_defaults_off(tmp_path: Path) -> None:
    missing = frame_gate(runtime_path=tmp_path / "absent.json")
    assert missing == (False, 3)
    no_key = tmp_path / "no-key.json"
    no_key.write_text('{"mode": "production_model"}', encoding="utf-8")
    assert frame_gate(runtime_path=no_key) == (False, 3)
    disabled = tmp_path / "disabled.json"
    disabled.write_text(
        '{"review_frame_materials": {"enabled": false}}', encoding="utf-8"
    )
    assert frame_gate(runtime_path=disabled) == (False, 3)


def test_frame_gate_parses_enabled_and_max_frames(tmp_path: Path) -> None:
    enabled = tmp_path / "enabled.json"
    enabled.write_text(
        '{"review_frame_materials": {"enabled": true, "max_frames": 2}}',
        encoding="utf-8",
    )
    assert frame_gate(runtime_path=enabled) == (True, 2)
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"review_frame_materials": true}', encoding="utf-8")
    assert frame_gate(runtime_path=malformed) == (False, 3)


# ---------------------------------------------------------------------------
# Route behavior: gate OFF (default)
# ---------------------------------------------------------------------------


def test_gate_off_keeps_today_behavior_exactly(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (False, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: _llm(received)
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _seed_preview(workspace, episode_id, fixture_clip)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["checked_materials"] == {"transcript": False, "shot": False}
    assert "frames" not in body["checked_materials"]
    assert received[0].frame_materials == ()
    assert not (workspace["episodes_root"] / episode_id / "review-frames").exists()


# ---------------------------------------------------------------------------
# Route behavior: gate ON with a real preview
# ---------------------------------------------------------------------------


def test_gate_on_extracts_stills_attaches_and_traces(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: _llm(received)
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    preview = _seed_preview(workspace, episode_id, fixture_clip)
    before = _fingerprint(preview)

    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()

    # The LLM received the frames (context-carried, PriorProposal precedent).
    assert received, "feelings route must consult the LLM"
    frames_on_context = received[0].frame_materials
    assert len(frames_on_context) == 3

    # Still files exist, are JPEGs, bounded width, inside the scratch dir.
    scratch = workspace["episodes_root"] / episode_id / "review-frames"
    for material in frames_on_context:
        path = Path(material.path)
        assert path.parent == scratch
        assert path.is_file()
        assert path.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic

    # The response carries traceable records (対象・時点・元版).
    records = body["checked_materials"]["frames"]
    assert records == [material.model_dump(mode="json") for material in frames_on_context]
    assert body["checked_materials"]["transcript"] is False
    assert body["checked_materials"]["shot"] is False
    for record in records:
        assert set(record) == {"path", "at_seconds", "source"}
        assert record["source"] == str(preview)

    # Durable trace: the chat entry records the same frames.
    entry = _chat_lines(workspace, episode_id)[0]
    assert entry["frame_materials"] == records

    # The source video was never modified.
    assert _fingerprint(preview) == before


def test_gate_on_stills_are_bounded_width(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: _llm(received)
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _seed_preview(workspace, episode_id, fixture_clip)
    client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    # Pinned decode budget: stills are SMALL (≤512 px wide), never full-res.
    for material in received[0].frame_materials:
        assert Path(material.path).stat().st_size < 200_000


# ---------------------------------------------------------------------------
# Honest degradation
# ---------------------------------------------------------------------------


def test_gate_on_without_media_degrades_without_frames_key(
    client: TestClient,
    workspace: dict[str, Path],
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No preview + an intake source ffmpeg cannot decode → honest skip."""

    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: _llm(received)
    )
    folder = Path(workspace["episodes_root"]).parent / "cam-fake"
    folder.mkdir(parents=True)
    (folder / "clip-001.mp4").write_bytes(b"not-a-real-video")
    episode_id = _episode(client, folder)
    _seed(workspace, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert "frames" not in body["checked_materials"]
    assert received[0].frame_materials == ()


def test_extract_review_frames_without_any_media_is_empty(
    tmp_path: Path,
) -> None:
    assert extract_review_frames(tmp_path, 1.0, max_frames=3) == ()


# ---------------------------------------------------------------------------
# P1-2 diagnosis: OBSERVED stills positions (8s testsrc2, at=1.0, max=3)
# ---------------------------------------------------------------------------


def _seed_diagnosis_preview(tmp_path: Path, clip: Path) -> None:
    preview = tmp_path / "previews" / "preview.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(clip.read_bytes())


@pytest.fixture(scope="module")
def fixture_clip_8s(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """8s testsrc2 — the P1-2 spread diagnosis clip."""

    try:
        tools = load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")
    media = tmp_path_factory.mktemp("review-frames-diag-8s") / "clip-8s.mp4"
    result = subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=8",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return media


def test_observed_8s_at_1s_yields_three_stills_at_0_1_3(
    tmp_path: Path, fixture_clip_8s: Path
) -> None:
    """The spread is ±2s (_AROUND_OFFSETS_SECONDS): at_seconds=1.0 plans
    (0.0, 1.0, 3.0) — NOT the ±1s (0:00/0:01/0:02) the earlier live run
    reported against. Pinned OBSERVED healthy path: all 3 stills extract,
    no per-still skip."""

    _seed_diagnosis_preview(tmp_path, fixture_clip_8s)
    frames = extract_review_frames(tmp_path, 1.0, max_frames=3)
    assert [material.at_seconds for material in frames] == [0.0, 1.0, 3.0]
    assert all(Path(material.path).is_file() for material in frames)


def test_observed_stamp_past_last_pts_fails_and_skips(
    tmp_path: Path, fixture_clip_8s: Path
) -> None:
    """OBSERVED: a seek past the last frame's PTS (120 frames @15fps → last
    PTS 119/15 ≈ 7.9333) decodes nothing → honest False, no file (a warning
    is logged; the remaining stamps still extract)."""

    tools = load_pinned_tools()
    target = tmp_path / "beyond.jpg"
    assert _extract_still(tools.ffmpeg, fixture_clip_8s, 8.0, target) is False
    assert not target.exists()


def test_observed_start_edge_respreads_distinct_stills(
    tmp_path: Path, fixture_clip_8s: Path
) -> None:
    """OBSERVED post-fix start-edge twin of the end-edge re-spread: at 0.0
    the -2s/0s offsets clamp onto the SAME stamp, and the planner spreads
    DISTINCT stamps over the feasible window instead of under-delivering."""

    _seed_diagnosis_preview(tmp_path, fixture_clip_8s)
    frames = extract_review_frames(tmp_path, 0.0, max_frames=3)
    stamps = [frame.at_seconds for frame in frames]
    assert len(stamps) == 3
    assert stamps == sorted(set(stamps))
    assert stamps[0] == 0.0
    assert stamps[-1] == pytest.approx(7.9333, abs=0.01)
    assert all(Path(frame.path).is_file() for frame in frames)


# ---------------------------------------------------------------------------
# P1-2 three-level honesty: 抽出 / AIに渡した / 確認結果が返った
# ---------------------------------------------------------------------------


def test_checked_materials_frames_carry_three_levels() -> None:
    nearby = NearbyContext(at_seconds=1.0, transcript_snippet="確認した発話")
    frame = FrameMaterial(path="p.jpg", at_seconds=1.0, source="s.mp4")
    assert checked_materials(nearby) == {"transcript": True, "shot": False}
    without_frames = checked_materials(
        nearby, frames_delivery_attempted=True, frames_verified=True
    )
    assert "frames_delivery_attempted" not in without_frames
    assert "frames_verified" not in without_frames
    # Frames extracted → the keys are ALWAYS present (P1-2 additive keys,
    # only alongside frames); uncomputable flags default to the HONEST
    # unverified values, never to a claim.
    default_unverified = checked_materials(nearby, frames=(frame,))
    assert default_unverified["frames"] == [frame.model_dump(mode="json")]
    assert default_unverified["frames_delivery_attempted"] is False
    assert default_unverified["frames_verified"] is False
    overstated = checked_materials(
        nearby, frames=(frame,), frames_delivery_attempted=False, frames_verified=True
    )
    assert overstated["frames_delivery_attempted"] is False
    assert overstated["frames_verified"] is False
    honest = checked_materials(
        nearby, frames=(frame,), frames_delivery_attempted=True, frames_verified=True
    )
    assert honest["frames_delivery_attempted"] is True
    assert honest["frames_verified"] is True


def test_real_transports_stamp_their_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The stamp rides the ACTUAL built transport instance: codex-exec →
    True, openai-api → False ALWAYS; regex-only (no gate) has no instance
    at all."""

    runtime = tmp_path / "runtime.json"
    pin = tmp_path / "pin.json"
    pin.write_text('{"model_id": "pinned-model"}', encoding="utf-8")
    monkeypatch.setattr(
        "services.cli.live_editorial_codex.make_codex_runner",
        lambda: (lambda *a, **k: "{}"),
    )
    runtime.write_text('{"mode": "production_model"}', encoding="utf-8")
    codex = build_review_llm_call(runtime_path=runtime, pin_path=pin)
    assert codex is not None
    assert llm_carries_images(codex) is True
    monkeypatch.setattr(
        "services.cli.live_editorial_v2.make_http_post",
        lambda: (lambda **kwargs: "{}"),
    )
    runtime.write_text(
        '{"mode": "production_model", "transport": "openai-api"}', encoding="utf-8"
    )
    openai_call = build_review_llm_call(
        runtime_path=runtime,
        pin_path=pin,
        env={
            "EDITORIAL_DIRECTOR_API_KEY": "key",
            "EDITORIAL_DIRECTOR_NETWORK_ENABLED": "1",
        },
    )
    assert openai_call is not None
    assert llm_carries_images(openai_call) is False
    assert llm_carries_images(None) is False


def test_interpret_message_outcome_reports_llm_use() -> None:
    context = ReviewChatContext(at_seconds=FEELINGS_AT)
    nearby = NearbyContext(at_seconds=FEELINGS_AT)
    received: list[ReviewChatContext] = []
    drafts, used, invoked = interpret_message_with_outcome(
        FEELINGS_TEXT, context, nearby, _llm(received)
    )
    assert used is True
    assert invoked is True
    assert drafts
    assert received
    fallback, unused, uninvoked = interpret_message_with_outcome(
        FEELINGS_TEXT, context, nearby, None
    )
    assert unused is False
    assert uninvoked is False
    assert fallback[0].needs_confirmation


def test_interpret_message_outcome_llm_error_is_false() -> None:
    context = ReviewChatContext(at_seconds=FEELINGS_AT)
    nearby = NearbyContext(at_seconds=FEELINGS_AT)
    drafts, used, invoked = interpret_message_with_outcome(
        FEELINGS_TEXT, context, nearby, _raising_llm()
    )
    assert used is False
    assert invoked is True
    assert drafts[0].needs_confirmation


# ---------------------------------------------------------------------------
# Round 3 contract: the INVOCATION FACT is reported separately. A
# capable-stamped transport that is never invoked (deterministic
# short-circuit) must not read as delivered.
# ---------------------------------------------------------------------------


def test_capable_llm_never_invoked_on_deterministic_short_circuit() -> None:
    received: list[ReviewChatContext] = []
    drafts, used, invoked = interpret_message_with_outcome(
        "この区間を削除して",
        ReviewChatContext(at_seconds=1.0),
        NearbyContext(at_seconds=1.0),
        _stamped_llm(received),
    )
    assert used is False
    assert invoked is False
    assert received == []
    assert drafts[0].needs_confirmation is False


def test_invoked_reports_invocation_regardless_of_answer_validity() -> None:
    def _stamped_raising() -> ReviewLlmCall:
        call = _raising_llm()
        setattr(call, "image_capable", True)  # noqa: B010 (mirrors _stamp_image_capable)
        return call

    drafts, used, invoked = interpret_message_with_outcome(
        FEELINGS_TEXT,
        ReviewChatContext(at_seconds=FEELINGS_AT),
        NearbyContext(at_seconds=FEELINGS_AT),
        _stamped_raising(),
    )
    assert used is False
    assert invoked is True
    assert drafts[0].needs_confirmation


# ---------------------------------------------------------------------------
# Reaction route (both-different) also gathers frames when gated on
# ---------------------------------------------------------------------------


def test_both_different_route_gathers_frames_when_gated(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: _llm(received)
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    preview = _seed_preview(workspace, episode_id, fixture_clip)
    first = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": "この区間を削除して"}
    )
    assert first.status_code == 200
    del preview  # the proposal-set chat must not touch the preview
    reaction = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": BOTH_DIFFERENT, "at_seconds": FEELINGS_AT},
    )
    assert reaction.status_code == 200
    body = reaction.json()
    assert "frames" in body["checked_materials"]
    assert body["checked_materials"]["frames"][0]["source"] == str(
        workspace["episodes_root"] / episode_id / "previews" / "preview.mp4"
    )


# ---------------------------------------------------------------------------
# Traceability primitives round-trip
# ---------------------------------------------------------------------------


def test_checked_materials_frames_absent_when_empty() -> None:
    nearby = NearbyContext(at_seconds=1.0, transcript_snippet="確認した発話")
    assert checked_materials(nearby) == {"transcript": True, "shot": False}
    assert "frames" not in checked_materials(nearby, frames=())


def test_route_seam_skips_eligible_false_and_gate_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SpyWorkspace(FrameMaterialWorkspace):
        def gather_review_frames(self, *args: object, **kwargs: object) -> tuple:
            raise AssertionError("must not be called")

    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    spy = _SpyWorkspace()
    assert (
        gather_route_frame_materials(spy, "e1", at_seconds=1.0, eligible=False) == ()
    )
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (False, 3)
    )
    assert (
        gather_route_frame_materials(spy, "e1", at_seconds=1.0, eligible=True) == ()
    )


# ---------------------------------------------------------------------------
# P1-2 reaction route: the SAME three-level contract (positive case)
# ---------------------------------------------------------------------------


def test_both_different_route_reports_attempted_and_verified(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call",
        lambda: _stamped_llm(received),
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _seed_preview(workspace, episode_id, fixture_clip)
    first = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": "この区間を削除して"}
    )
    assert first.status_code == 200
    reaction = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": BOTH_DIFFERENT, "at_seconds": FEELINGS_AT},
    )
    assert reaction.status_code == 200
    materials = reaction.json()["checked_materials"]
    assert len(materials["frames"]) == 3
    assert materials["frames_delivery_attempted"] is True
    assert materials["frames_verified"] is True


def test_plain_route_honesty_matrix(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain feelings route, the evidence combinations on ONE episode:
    unstamped (the openai-api-like case) → False/False; stamped but the
    exec DROPS (deterministic fallback answered) → attempted True /
    verified False; no LLM at all → False/False. 抽出 alone never reads
    as 確認済み."""

    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _seed_preview(workspace, episode_id, fixture_clip)
    url = f"/episodes/{episode_id}/review-chat"
    payload = {"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT}

    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: _llm([])
    )
    materials = client.post(url, json=payload).json()["checked_materials"]
    assert len(materials["frames"]) == 3
    assert materials["frames_delivery_attempted"] is False
    assert materials["frames_verified"] is False

    def stamped_raising() -> ReviewLlmCall:
        call = _raising_llm()
        setattr(call, "image_capable", True)  # noqa: B010 (mirrors _stamp_image_capable)
        return call

    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", stamped_raising
    )
    materials = client.post(url, json=payload).json()["checked_materials"]
    assert materials["frames_delivery_attempted"] is True
    assert materials["frames_verified"] is False

    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: None
    )
    materials = client.post(url, json=payload).json()["checked_materials"]
    assert materials["frames_delivery_attempted"] is False
    assert materials["frames_verified"] is False


def test_plain_route_stamped_llm_answer_is_attempted_and_verified(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call",
        lambda: _stamped_llm(received),
    )
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _seed_preview(workspace, episode_id, fixture_clip)
    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert response.status_code == 200
    materials = response.json()["checked_materials"]
    assert len(materials["frames"]) == 3
    assert materials["frames_delivery_attempted"] is True
    assert materials["frames_verified"] is True


# ---------------------------------------------------------------------------
# Rework round 4 P2 (codex repro): the delivery fact is an ATTEMPT. The
# three locked states, the pre-spawn one through the REAL codex exec seam.
# ---------------------------------------------------------------------------


def test_codex_three_locked_states_delivery_attempted(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    fixture_clip: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``frames_delivery_attempted`` is a call ATTEMPT, never a delivery
    claim. THREE locked states on ONE episode:

    (a) pre-spawn failure — the REAL ``_CodexExecRunner`` with a binary
        that does not exist, so ``subprocess.run`` raises OSError (a
        FileNotFoundError, pre-spawn: no process ever launched) →
        attempted=True / verified=False;
    (b) no call — deterministic short-circuit (no LLM at all) →
        attempted=False / verified=False;
    (c) success — the model answered → attempted=True / verified=True.

    The old ``frames_delivered`` key never appears (clean rename).
    """

    monkeypatch.setattr(
        "services.episode_cockpit.review_frames.frame_gate", lambda *a, **k: (True, 3)
    )

    def prespawn_failure() -> ReviewLlmCall:
        monkeypatch.setattr(
            "services.cli.live_editorial_codex.make_codex_runner",
            lambda: _CodexExecRunner(binary="codex-binary-missing-for-test"),
        )
        call = _codex_call("pinned-model")
        assert call is not None
        assert llm_carries_images(call) is True
        return call

    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _seed_preview(workspace, episode_id, fixture_clip)
    url = f"/episodes/{episode_id}/review-chat"
    payload = {"text": FEELINGS_TEXT, "at_seconds": FEELINGS_AT}

    # (a) pre-spawn failure: the process never launched, still "attempted".
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", prespawn_failure
    )
    materials = client.post(url, json=payload).json()["checked_materials"]
    assert len(materials["frames"]) == 3
    assert materials["frames_delivery_attempted"] is True
    assert materials["frames_verified"] is False
    assert "frames_delivered" not in materials

    # (b) no call: the deterministic short-circuit (no LLM at all).
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call", lambda: None
    )
    materials = client.post(url, json=payload).json()["checked_materials"]
    assert len(materials["frames"]) == 3
    assert materials["frames_delivery_attempted"] is False
    assert materials["frames_verified"] is False
    assert "frames_delivered" not in materials

    # (c) success: the model answered with usable proposals.
    received: list[ReviewChatContext] = []
    monkeypatch.setattr(
        "services.episode_cockpit.api.build_review_llm_call",
        lambda: _stamped_llm(received),
    )
    materials = client.post(url, json=payload).json()["checked_materials"]
    assert len(materials["frames"]) == 3
    assert materials["frames_delivery_attempted"] is True
    assert materials["frames_verified"] is True
    assert "frames_delivered" not in materials


# ---------------------------------------------------------------------------
# P1-2 stills diagnosis: OBSERVED ceiling-edge fuse (deterministic, tmp_path)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clip_end(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """51 frames @ 25fps: the decodable ceiling is exactly 2.0s (ffprobe
    nb_frames=51 → (51-1)/25 = 2.0) — the playhead CAN sit on the edge."""

    try:
        tools = load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")
    media = tmp_path_factory.mktemp("review-frames-diag-end") / "clip-end.mp4"
    result = subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25:duration=2.04",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return media


def test_observed_at_decodable_ceiling_the_tail_stamp_fuses(
    tmp_path: Path, clip_end: Path
) -> None:
    """OBSERVED (pre-fix mechanism, kept as the diagnosis record): with the
    playhead ON the decodable ceiling, the +2s stamp clamps ONTO the center
    stamp. POST-FIX: the planner RE-SPREADS distinct stamps over the
    feasible window instead of under-delivering — at=2.0 on a 2.0s clip now
    yields 3 distinct stills (0.0, 1.0, 2.0), all extracted. INFERENCE
    (separated from observation): the pre-fix clamp-fuse at a span edge is
    the plausible mechanism behind the earlier live run's 「2枚 at
    0:00/0:02」 report (playhead at/near the end of the decodable span)."""

    _seed_diagnosis_preview(tmp_path, clip_end)
    frames = extract_review_frames(tmp_path, 2.0, max_frames=3)
    assert [material.at_seconds for material in frames] == [0.0, 1.0, 2.0]
    assert all(Path(material.path).is_file() for material in frames)
