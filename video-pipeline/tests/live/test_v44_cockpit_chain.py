"""Tier C live cockpit chain test (task 10): real runner, no seeding.

Gated by ``V44_LIVE=1`` (the ``resolve_live`` skip-with-reason precedent):
this file drives the REAL pipeline end to end — POST /episodes detaches a
real ``services.cli.episode_runner`` subprocess (ingest → normalize → REAL
pinned whisper ASR → heuristic-director selection → plan → compile →
preview) against the committed cockpit fixture clip, entirely through the
FastAPI surface against a real tmp workspace. Nothing seeds PREVIEW_READY,
stage rows, the preview file, or the review store.

The default editorial runtime is an explicit ``heuristic_diagnostic``
config (no credentials needed); the blocked variant pins
``production_model`` with cleared credentials and proves the honest
``failed_blocked`` / ``production-model-unavailable`` refusal.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

# The detached runner Popen is intentionally never waited on (POST returns
# in O(ms)); its __del__ ResourceWarning under filterwarnings=error is a
# false alarm for this spawn pattern — ignored ONLY in this live module.
pytestmark = [
    pytest.mark.v44_live,
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
]

SKIP_REASON = (
    "V44_LIVE=1 required — live real-chain Tier C test "
    "(real runner subprocess + pinned whisper toolchain)"
)
CHAIN_DEADLINE_S = 600.0
REBUILD_DEADLINE_S = 300.0
GATE_DEADLINE_S = 60.0
POLL_INTERVAL_S = 2.0
REAL_STAGES = frozenset(
    ("intake", "ingest", "normalize", "analyze", "selection", "plan", "preview")
)
CREDENTIAL_VARS = ("EDITORIAL_DIRECTOR_API_KEY", "EDITORIAL_DIRECTOR_NETWORK_ENABLED")
FIXTURE_CLIP = (
    Path(__file__).resolve().parents[2].parent
    / "cockpit"
    / "tests"
    / "e2e"
    / "fixtures"
    / "live-e2e-ja-speech.mp4"
)
LOCK_PATH = Path("config/toolchains/phase-1-technical-v1.json")


def _require_live_env() -> None:
    if os.environ.get("V44_LIVE") != "1":
        pytest.skip(SKIP_REASON)


def _require_bootstrapped_toolchain() -> Phase1TechnicalToolchainLock:
    lock = load_lock(LOCK_PATH)
    assert isinstance(lock, Phase1TechnicalToolchainLock)
    missing = [
        str(path)
        for path in (
            Path(lock.ffmpeg.ffmpeg.path),
            Path(lock.ffmpeg.ffprobe.path),
            Path(lock.whisper_ja.whisper_cli.path),
            Path(lock.whisper_ja.model.path),
        )
        if not path.is_file()
    ]
    if missing:
        pytest.skip(f"pinned real-chain toolchain not bootstrapped: {missing}")
    return lock


class LiveWorkspace:
    """One real cockpit workspace (episodes root + StateStore) in tmp."""

    def __init__(self, root: Path, mode: str, transport: str | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.episodes_root = root / "episodes"
        self.state_store = root / "state.db"
        runtime = root / "editorial-runtime.json"
        # transport=None keeps the shipped default (codex-exec); the blocked
        # variant pins openai-api so the test's "no credentials" premise
        # matches the transport it gates on.
        config: dict[str, str] = {"mode": mode}
        if transport is not None:
            config["transport"] = transport
        runtime.write_text(json.dumps(config), encoding="utf-8")
        self.runtime_config = runtime
        self.app = create_cockpit_app(
            state_store_path=self.state_store, episodes_root=self.episodes_root
        )

    def episode_dir(self, episode_id: str) -> Path:
        return self.episodes_root / episode_id

    def runner_log(self, episode_id: str) -> str:
        path = self.episode_dir(episode_id) / "runner.log"
        if not path.is_file():
            return f"<no runner.log at {path}>"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        return "<runner.log tail>\n" + "\n".join(lines[-80:])


@dataclass(frozen=True, slots=True)
class PollRequest:
    """One bounded-poll target: an episode's status inside one workspace."""

    client: TestClient
    episode_id: str
    workspace: LiveWorkspace


def _get_status(client: TestClient, episode_id: str) -> dict[str, Any]:
    response = client.get(f"/episodes/{episode_id}")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _blocked_row(status: dict[str, Any]) -> dict[str, Any] | None:
    for run in status["stage_runs"]:
        if run["status"] == "failed_blocked":
            return run
    return None


def _poll(
    poll: PollRequest,
    *,
    deadline_s: float,
    done: Callable[[dict[str, Any]], bool],
    what: str,
) -> dict[str, Any]:
    """Bounded poll; a blocked stage or a timeout fails with runner.log."""

    deadline = time.monotonic() + deadline_s
    status: dict[str, Any] = {}
    while True:
        status = _get_status(poll.client, poll.episode_id)
        blocked = _blocked_row(status)
        if blocked is not None:
            pytest.fail(
                f"{what}: stage {blocked['stage_name']} failed_blocked "
                f"({blocked['last_error_code']})\n{poll.workspace.runner_log(poll.episode_id)}",
                pytrace=False,
            )
        if done(status):
            return status
        if time.monotonic() > deadline:
            pytest.fail(
                f"{what}: not reached within {deadline_s}s "
                f"(status={status.get('status')} stage={status.get('current_stage')})"
                f"\n{poll.workspace.runner_log(poll.episode_id)}",
                pytrace=False,
            )
        time.sleep(POLL_INTERVAL_S)


def _metrics_lines(workspace: LiveWorkspace, episode_id: str) -> int:
    path = workspace.episode_dir(episode_id) / "rebuild-metrics.jsonl"
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def test_live_chain_intake_to_preview_and_partial_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_live_env()
    _require_bootstrapped_toolchain()
    workspace = LiveWorkspace(tmp_path / "live", "heuristic_diagnostic")
    monkeypatch.setenv("EDITORIAL_RUNTIME_CONFIG", str(workspace.runtime_config))
    for var in CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)

    source = tmp_path / "cam-live"
    source.mkdir()
    shutil.copyfile(FIXTURE_CLIP, source / "camera-001.mp4")

    with TestClient(workspace.app) as client:
        started = time.monotonic()
        created = client.post(
            "/episodes",
            json={"source_folder": str(source), "brief_text": "Tier C live: 実チェーン"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["pipeline"] == "started"
        episode_id = created.json()["episode_id"]
        episode_dir = workspace.episode_dir(episode_id)

        status = _poll(
            PollRequest(client, episode_id, workspace),
            deadline_s=CHAIN_DEADLINE_S,
            done=lambda payload: payload["status"] == "PREVIEW_READY",
            what="real chain to PREVIEW_READY",
        )
        chain_wall = time.monotonic() - started

        succeeded = {
            run["stage_name"] for run in status["stage_runs"] if run["status"] == "succeeded"
        }
        assert succeeded >= REAL_STAGES, status["stage_runs"]
        assert len(status["stage_runs"]) >= 7
        preview = episode_dir / "previews" / "preview.mp4"
        assert preview.is_file(), "preview artifact must exist on disk"

        chat = client.post(
            f"/episodes/{episode_id}/review-chat",
            json={"text": "0:02の区間を削除して", "at_seconds": None},
        )
        assert chat.status_code == 200, chat.text
        draft = chat.json()["draft"]
        assert draft["command_kind"] == "remove_section"
        assert draft["needs_confirmation"] is False
        assert draft["target_seconds"] == 2.0

        applied = client.post(
            f"/episodes/{episode_id}/review-chat/apply",
            json={"text": "0:02の区間を削除して", "at_seconds": None},
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()["applied"]
        assert applied_body["deferred"] is False

        rebuild_started = time.monotonic()
        rebuild = client.post(
            f"/episodes/{episode_id}/rebuild",
            json={"applied_command": applied_body["command_id"]},
        )
        assert rebuild.status_code == 202, rebuild.text
        rebuild_body = rebuild.json()
        assert rebuild_body["scheduled"] is True
        assert "plan" in rebuild_body["stages"]

        _poll(
            PollRequest(client, episode_id, workspace),
            deadline_s=REBUILD_DEADLINE_S,
            done=lambda payload: _metrics_lines(workspace, episode_id) >= 1,
            what="partial rebuild to a fresh rebuild-metrics line",
        )
        rebuild_wall = time.monotonic() - rebuild_started

        assert preview.is_file(), "preview must still exist after the rebuild"
        log = workspace.runner_log(episode_id)
        assert "rebuild_finished" in log
        print(f"[live] chain_wall={chain_wall:.1f}s rebuild_wall={rebuild_wall:.1f}s")


def test_live_production_mode_without_credentials_blocks_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_live_env()
    # openai-api transport: the no-credentials premise is exactly what this
    # gate blocks (the codex-exec default gates on the codex probe instead).
    workspace = LiveWorkspace(tmp_path / "blocked", "production_model", transport="openai-api")
    monkeypatch.setenv("EDITORIAL_RUNTIME_CONFIG", str(workspace.runtime_config))
    for var in CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)

    source = tmp_path / "cam-blocked"
    source.mkdir()
    shutil.copyfile(FIXTURE_CLIP, source / "camera-001.mp4")

    with TestClient(workspace.app) as client:
        created = client.post(
            "/episodes",
            json={"source_folder": str(source), "brief_text": "Tier C blocked variant"},
        )
        assert created.status_code == 200, created.text
        episode_id = created.json()["episode_id"]

        deadline = time.monotonic() + GATE_DEADLINE_S
        blocked: dict[str, Any] | None = None
        while True:
            status = _get_status(client, episode_id)
            blocked = _blocked_row(status)
            if blocked is not None:
                break
            if time.monotonic() > deadline:
                pytest.fail(
                    "production gate never blocked:\n"
                    + workspace.runner_log(episode_id),
                    pytrace=False,
                )
            time.sleep(POLL_INTERVAL_S)

        assert blocked is not None
        assert blocked["last_error_code"] == "production-model-unavailable"
        assert status["status"] == "CREATED"
        assert "production-model-unavailable" in workspace.runner_log(episode_id)
