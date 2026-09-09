"""Task 7: intake launches the real pipeline via the detached one-shot runner.

Tier A, in-process. The chain entry (``run_real_chain`` as imported by
the runner) is monkeypatched with fast fakes that drive the SAME
StateStore machinery the real chain drives; nothing is ever seeded —
every stage run the assertions see was recorded by the runner through
StateStore/apply_transition. Crash containment runs the real runner CLI
in a subprocess whose fake chain hard-dies (``os._exit(137)``) so the
StateStore's last recorded stage provably stands.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.cli import episode_runner, episode_runner_editorial
from services.cli.bundle import ReviewTarget, assemble_real_bundle, save_bundle
from services.cli.episode_runner_state import RunContext
from services.cli.live_editorial_codex import CodexTransportGatedError
from services.cli.real_chain import RealChainError
from services.episode_cockpit import episode_ops
from services.episode_cockpit.app import create_cockpit_app
from services.foundation_io import sha256_file
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import MAIN_PATH

if TYPE_CHECKING:
    from collections.abc import Iterator

PIPELINE_ROOT = Path(episode_ops.__file__).resolve().parents[2]
CHAIN_JOB = "job-real-episode-run"


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {
        "state_store": tmp_path / "state.db",
        "episodes_root": tmp_path / "jobs",
    }


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


def _create_episode(client: TestClient, source_folder: Path) -> dict[str, object]:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "travel vlog, calm"},
    )
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def _fake_chain_factory(
    captured: dict[str, object],
) -> Callable[..., None]:
    """Build a fake chain driving the chain's own StateStore store to PREVIEW_READY."""

    def fake_chain(  # noqa: PLR0913 (mirrors the run_real_chain seam)
        episode_root: Path, stop: str, out_dir: Path, *, env: dict[str, str] | None = None,
        policy_path: Path | None = None, editorial_runtime: Path | None = None,
    ) -> None:
        captured["env"] = dict(env or {})
        out_dir.mkdir(parents=True, exist_ok=True)
        with StateStore.open(out_dir / "state.sqlite3") as chain:
            chain.create_job(
                job_id=CHAIN_JOB, episode_id="runner-fake", current_stage="editorial"
            )
            for index, status in enumerate(MAIN_PATH[1:7]):
                snapshot = current_job_state(chain, CHAIN_JOB)
                apply_transition(
                    chain,
                    CHAIN_JOB,
                    expected_status=snapshot.status,
                    expected_parent_hash=snapshot.adopted_artifact_hash,
                    new_status=status,
                    new_artifact_hash=f"{index + 1:064x}",
                )
        preview = out_dir / "preview-v1"
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "preview.mp4").write_bytes(b"fake-preview")
        save_bundle(
            assemble_real_bundle(
                episode_id=episode_root.name,
                eligibility_status="supported",
                mezzanine_sha256="0" * 64,
                edit_source_world_sha256="0" * 64,
                episode_manifest_sha256="0" * 64,
                policy_sha256="0" * 64,
                target=ReviewTarget(
                    plan_version="v1",
                    plan_sha256="0" * 64,
                    ir_sha256="0" * 64,
                    preview_dir="preview-v1",
                    preview_sha256=sha256_file(preview / "preview.mp4"),
                    trace_sha256="0" * 64,
                ),
            ),
            out_dir / "review-bundle.json",
        )

    return fake_chain


def _runner_log_events(episode_dir: Path) -> list[dict[str, object]]:
    lines = (episode_dir / "runner.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


# ---------------------------------------------------------------------------
# (1a) POST /episodes returns fast, spawns the detached runner with the
#      exact Popen argument contract, and persists the intake record.
# ---------------------------------------------------------------------------


def test_post_create_spawns_detached_runner_with_expected_args(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    started = time.monotonic()
    body = _create_episode(client, source_folder)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # spawn-and-return; never waits on the chain
    assert body["pipeline"] == "started"
    assert body["status"] == "CREATED"
    episode_dir = workspace["episodes_root"] / str(body["episode_id"])

    assert len(runner_spawn_calls) == 1
    spawn = runner_spawn_calls[0]
    argv = cast("list[str]", spawn["argv"])
    assert argv[:9] == [
        sys.executable,
        "-m",
        "services.cli.episode_runner",
        "--episode-root",
        str(episode_dir),
        "--stop",
        "PREVIEW_READY",
        "--state-store",
        str(workspace["state_store"]),
    ]
    assert argv[argv.index("--runner-lock-fd") + 1].isdigit()
    assert spawn["cwd"] == PIPELINE_ROOT
    assert spawn["start_new_session"] is True
    assert spawn["stdout"] is spawn["stderr"]

    intake = json.loads((episode_dir / "intake.json").read_bytes())
    assert intake["episode_id"] == body["episode_id"]
    assert intake["source_folder"] == str(source_folder.resolve())
    assert intake["brief_text"] == "travel vlog, calm"
    assert intake["created_at"]


# ---------------------------------------------------------------------------
# (1b) the in-process run() path reaches PREVIEW_READY with >=7 stage runs
#      and NO seeded writes — every row comes from the runner itself.
# ---------------------------------------------------------------------------


def test_run_advances_episode_to_preview_ready(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    # Leaked credentials must never turn a config-absent diagnostic run live.
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "sk-leaked-must-be-stripped")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-leaked-must-be-stripped")
    monkeypatch.setenv("GEMINI_NETWORK_ENABLED", "1")
    monkeypatch.setenv("ZAI_API_KEY", "zai-leaked-must-be-stripped")
    monkeypatch.setenv("ZAI_NETWORK_ENABLED", "1")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        episode_runner, "run_real_chain", _fake_chain_factory(captured)
    )

    body = _create_episode(client, source_folder)
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id
    with StateStore.open(workspace["state_store"]) as store:
        assert store.get_job_snapshot(episode_id).stage_runs == ()  # no seeds

    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
    )

    assert exit_code == episode_runner.EXIT_SUCCESS
    chain_env = cast("dict[str, str]", captured["env"])
    assert "EDITORIAL_DIRECTOR_API_KEY" not in chain_env  # diagnostic never goes live
    assert "EDITORIAL_DIRECTOR_NETWORK_ENABLED" not in chain_env
    assert "GEMINI_API_KEY" not in chain_env
    assert "GEMINI_NETWORK_ENABLED" not in chain_env
    assert "ZAI_API_KEY" not in chain_env
    assert "ZAI_NETWORK_ENABLED" not in chain_env
    status = client.get(f"/episodes/{episode_id}").json()
    assert status["status"] == "PREVIEW_READY"
    stage_runs = status["stage_runs"]
    assert len(stage_runs) == 7
    assert [run["stage_name"] for run in stage_runs] == [
        "intake", "ingest", "normalize", "analyze", "selection", "plan", "preview",
    ]
    assert all(run["status"] == "succeeded" for run in stage_runs)
    assert all(run["last_error_code"] is None for run in stage_runs)
    assert (episode_dir / "episode.json").is_file()
    assert (episode_dir / "previews" / "preview.mp4").read_bytes() == b"fake-preview"

    events = _runner_log_events(episode_dir)
    kinds = [event["event"] for event in events]
    assert "runner_started" in kinds
    assert "editorial_mode" in kinds
    assert "chain_finished" in kinds
    assert kinds[-1] == "runner_finished"
    assert events[kinds.index("editorial_mode")]["mode"] == "heuristic_diagnostic"


def test_preview_published_event_carries_run_version_hash(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        episode_runner, "run_real_chain", _fake_chain_factory(captured)
    )

    body = _create_episode(client, source_folder)
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id
    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
    )

    assert exit_code == episode_runner.EXIT_SUCCESS
    events = _runner_log_events(episode_dir)
    started = next(event for event in events if event["event"] == "runner_started")
    published = next(
        event for event in events if event["event"] == "preview_published"
    )
    assert published["run_id"] == started["run_id"]
    assert published["target_version"] == "v1"
    assert published["content_hash"] == sha256_file(
        episode_dir / "previews" / "preview.mp4"
    )


# ---------------------------------------------------------------------------
# (2) blocked mode: production editorial runtime fails fast with the typed
#     error code per TRANSPORT — openai-api gates on the env vars (cleared
#     by the autouse hermetic fixture), codex-exec gates on the codex probe
#     (faked) — never a silent heuristic fallback either way.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["openai-api", "codex-exec"])
def test_production_mode_gate_blocks_per_transport(  # noqa: PLR0913, PLR0917 (six pytest fixtures, all used)
    transport: str,
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    if transport == "codex-exec":

        def gated() -> object:
            raise CodexTransportGatedError(
                "codex-not-logged-in", "Not logged in — run `codex login`"
            )

        monkeypatch.setattr(episode_runner_editorial, "make_codex_runner", gated)
    config = tmp_path / "editorial-runtime.json"
    config.write_text(json.dumps({"schema_version": "editorial-runtime-v1",
                                  "mode": "production_model",
                                  "transport": transport}))
    body = _create_episode(client, source_folder)
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id

    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        editorial_runtime=config,
    )

    assert exit_code == episode_runner.EXIT_BLOCKED
    status = client.get(f"/episodes/{episode_id}").json()
    blocked = [run for run in status["stage_runs"] if run["status"] == "failed_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["last_error_code"] == "production-model-unavailable"
    blocked_event = next(
        event for event in _runner_log_events(episode_dir) if event["event"] == "blocked"
    )
    assert blocked_event["code"] == "production-model-unavailable"
    if transport == "openai-api":
        assert "EDITORIAL_DIRECTOR_API_KEY" in str(blocked_event["detail"])
    else:
        assert "codex login" in str(blocked_event["detail"])


def test_production_mode_codex_probe_ok_proceeds_past_gate(
    workspace: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given: codex transport with a passing (faked) probe; Then: the gate
    proceeds — no block is recorded, proving production WITHOUT API keys is
    no longer blocked when codex is logged in."""

    monkeypatch.setattr(episode_runner_editorial, "make_codex_runner", object)
    config = tmp_path / "editorial-runtime.json"
    config.write_text(json.dumps({"schema_version": "editorial-runtime-v1",
                                  "mode": "production_model",
                                  "transport": "codex-exec"}))
    with (tmp_path / "gate.log").open("ab") as log:
        episode_runner_editorial.editorial_transport(config, log)
        with StateStore.open(workspace["state_store"]) as store:
            ctx = RunContext(
                workspace["state_store"], "job-gate-codex-ok", "run-1", "PREVIEW_READY", log
            )
            episode_runner_editorial.require_production_ready(store, ctx, "codex-exec")


def test_production_mode_with_gate_but_missing_provider_module_blocks(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "sk-test")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")
    monkeypatch.setattr(
        episode_runner_editorial,
        "PRODUCTION_RUNTIME_MODULE",
        "services.editorial_v2.not_yet_here",
    )
    config = tmp_path / "editorial-runtime.json"
    config.write_text(json.dumps({"schema_version": "editorial-runtime-v1",
                                  "mode": "production_model",
                                  "transport": "openai-api"}))
    body = _create_episode(client, source_folder)
    episode_dir = workspace["episodes_root"] / str(body["episode_id"])

    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        editorial_runtime=config,
    )

    assert exit_code == episode_runner.EXIT_BLOCKED
    with StateStore.open(workspace["state_store"]) as store:
        runs = store.get_job_snapshot(str(body["episode_id"])).stage_runs
    assert any(
        run.status == "failed_blocked"
        and run.last_error_code == "production-model-unavailable"
        for run in runs
    )
    blocked_event = next(
        event
        for event in _runner_log_events(episode_dir)
        if event["event"] == "blocked"
    )
    assert "not yet available" in str(blocked_event["detail"])


# ---------------------------------------------------------------------------
# (3a) crash containment: a hard os._exit(137) mid-chain leaves the last
#      StateStore-recorded stage standing; a re-POST 409s (no double runner).
# ---------------------------------------------------------------------------

CRASH_BOOTSTRAP = textwrap.dedent(
    """
    import os
    import time
    import services.cli.episode_runner as runner
    from services.job_runner.cas import apply_transition, current_job_state
    from services.job_runner.state_store import StateStore

    def crashing_chain(episode_root, stop, out_dir, *, env=None, policy_path=None,
                       editorial_runtime=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        with StateStore.open(out_dir / "state.sqlite3") as chain:
            chain.create_job(job_id="job-real-episode-run",
                             episode_id="runner-crash", current_stage="editorial")
            snapshot = current_job_state(chain, "job-real-episode-run")
            apply_transition(chain, "job-real-episode-run",
                             expected_status=snapshot.status,
                             expected_parent_hash=snapshot.adopted_artifact_hash,
                             new_status="INGESTED", new_artifact_hash="1" * 64)
        time.sleep(2.5)  # let the runner's watcher mirror INGESTED, then die hard
        os._exit(137)

    runner.run_real_chain = crashing_chain
    raise SystemExit(runner.main())
    """
)


def test_hard_crash_leaves_last_recorded_stage_and_prevents_double_runner(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    body = _create_episode(client, source_folder)
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASH_BOOTSTRAP,
            "--episode-root",
            str(episode_dir),
            "--stop",
            "PREVIEW_READY",
            "--state-store",
            str(workspace["state_store"]),
        ],
        cwd=PIPELINE_ROOT,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert crashed.returncode == 137
    with StateStore.open(workspace["state_store"]) as store:
        snapshot = store.get_job_snapshot(episode_id)
    assert snapshot.job.status == "INGESTED"  # last mirrored stage stands
    by_stage = {run.stage_name: run.status for run in snapshot.stage_runs}
    assert by_stage["intake"] == "succeeded"
    assert by_stage["ingest"] == "succeeded"
    assert by_stage["normalize"] == "running"  # honest stale in-flight row
    kinds = [event["event"] for event in _runner_log_events(episode_dir)]
    assert "runner_started" in kinds
    assert "runner_finished" not in kinds  # abrupt end is not papered over

    repeat = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "again"},
    )
    assert repeat.status_code == 409
    assert repeat.json()["error"]["code"] == "episode-exists"
    assert len(runner_spawn_calls) == 1  # the 409 guard IS the double-spawn guard


# ---------------------------------------------------------------------------
# (3b) typed chain failure: the frontier stage run goes failed_blocked with
#      the chain's own error code; exit 1.
# ---------------------------------------------------------------------------


def test_typed_chain_error_blocks_running_frontier(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)

    def failing_chain(  # noqa: PLR0913 (mirrors the run_real_chain seam)
        episode_root: Path, stop: str, out_dir: Path, *,
        env: dict[str, str] | None = None, policy_path: Path | None = None,
        editorial_runtime: Path | None = None,
    ) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with StateStore.open(out_dir / "state.sqlite3") as chain:
            chain.create_job(
                job_id=CHAIN_JOB, episode_id="runner-fail", current_stage="editorial"
            )
            for status in MAIN_PATH[1:4]:
                snapshot = current_job_state(chain, CHAIN_JOB)
                apply_transition(
                    chain,
                    CHAIN_JOB,
                    expected_status=snapshot.status,
                    expected_parent_hash=snapshot.adopted_artifact_hash,
                    new_status=status,
                    new_artifact_hash="a" * 64,
                )
        raise RealChainError("analyze_failed", "transient analyzer refusal")

    monkeypatch.setattr(episode_runner, "run_real_chain", failing_chain)
    body = _create_episode(client, source_folder)
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id

    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
    )

    assert exit_code == episode_runner.EXIT_BLOCKED
    with StateStore.open(workspace["state_store"]) as store:
        snapshot = store.get_job_snapshot(episode_id)
    assert snapshot.job.status == "ANALYZED"
    frontier = [
        run for run in snapshot.stage_runs if run.status == "failed_blocked"
    ]
    assert len(frontier) == 1
    assert frontier[0].stage_name == "selection"
    assert frontier[0].last_error_code == "analyze_failed"


# ---------------------------------------------------------------------------
# (4) malformed inputs exit 2 with a typed log line.
# ---------------------------------------------------------------------------


def test_missing_intake_record_is_malformed(
    workspace: dict[str, Path], tmp_path: Path
) -> None:
    empty_root = tmp_path / "not-an-episode"
    empty_root.mkdir()

    exit_code = episode_runner.run(
        episode_root=empty_root,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
    )

    assert exit_code == episode_runner.EXIT_MALFORMED
    malformed = next(
        event
        for event in _runner_log_events(empty_root)
        if event["event"] == "malformed"
    )
    assert malformed["code"] == "intake-missing"
