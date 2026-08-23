"""Task 9: real partial rebuild — applied command → affected stages → preview.

Tier A, in-process (the task-7 fake-chain pattern): the initial run uses
a fake chain that drives the REAL StateStore machinery to PREVIEW_READY
and leaves an honest review-store + bundle behind, so the task-9 surface
under test is exactly the production one — apply commits a sealed v2 via
the deterministic path, POST /rebuild derives the lineage stage set and
detaches the runner (Popen stub), and the in-process ``--from-stage``
re-entry executes plan/compile/preview against the CURRENT committed
store. Nothing is seeded past what the chain itself would have written.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.cli import episode_runner, episode_runner_rebuild
from services.cli.bundle import (
    ReviewTarget,
    assemble_real_bundle,
    load_bundle,
    save_bundle,
)
from services.cli.episode_runner_workspace import publish_preview
from services.cli.project import init_review_store, plan_sha256
from services.cli.real_chain import JOB_ID as CHAIN_JOB
from services.cli.review_common import store_ir, store_plan
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.episode_cockpit import episode_ops
from services.episode_cockpit.app import create_cockpit_app
from services.foundation_io import sha256_file
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import MAIN_PATH
from services.preview.render import PREVIEW_NAME, TRACE_NAME
from services.review_command.store import HeadState, load_head

if TYPE_CHECKING:
    from typing import BinaryIO

    from services.contracts.timeline_ir import TimelineIr0C

PIPELINE_ROOT = Path(episode_ops.__file__).resolve().parents[2]
RATE = RationalFrameRate(num=30, den=1)
REMOVE_TEXT = "1秒のところを削除して"


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


def _seed_plan() -> EditPlan0C:
    def video(item_id: str, start: int, end: int) -> EditPlanItem0C:
        return EditPlanItem0C(
            item_id=item_id,
            kind="video",
            source_id="src-ep",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
            track_index=1,
        )

    return EditPlan0C(
        artifact_id="edit-plan-review-ep-seed",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="phase1-review-plane", version="1"),
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="src-ep", total_frames=60),
            items=(video("v1", 0, 30), video("v2", 30, 60)),
        ),
    )


def _fake_chain_factory(captured: dict[str, object]) -> Callable[..., None]:
    """Fake chain to PREVIEW_READY leaving a real review store + bundle."""

    def fake_chain(
        episode_root: Path, stop: str, out_dir: Path, *, env: dict[str, str] | None = None,
        policy_path: Path | None = None,
    ) -> None:
        captured["out_dir"] = out_dir
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
        mezzanine = out_dir / "media" / "edit-source.mov"
        mezzanine.parent.mkdir(parents=True, exist_ok=True)
        mezzanine.write_bytes(b"fake-mezzanine")
        preview_dir = out_dir / "preview-v1"
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / "preview.mp4").write_bytes(b"fake-preview-v1")
        seed = _seed_plan()
        store_dir = out_dir / "review-store"
        init_review_store(seed, store_dir)
        preview_sha = sha256_file(preview_dir / "preview.mp4")
        mezz_sha = sha256_file(mezzanine)
        save_bundle(
            assemble_real_bundle(
                episode_id=episode_root.name,
                eligibility_status="supported",
                mezzanine_sha256=mezz_sha,
                edit_source_world_sha256=mezz_sha,
                episode_manifest_sha256=mezz_sha,
                policy_sha256=mezz_sha,
                target=ReviewTarget(
                    plan_version="v1",
                    plan_sha256=plan_sha256(seed),
                    ir_sha256=sha256_file(store_dir / "ir-v1.json"),
                    preview_dir="preview-v1",
                    preview_sha256=preview_sha,
                    trace_sha256=preview_sha,
                ),
            ),
            out_dir / "review-bundle.json",
        )

    return fake_chain


def _fake_stage_preview(
    episode_root: Path, head: HeadState, plan: EditPlan0C, ir: TimelineIr0C, log: BinaryIO
) -> str:
    """Rebuild preview seam: write version-tagged bytes + republish (no ffmpeg)."""

    preview_dir = episode_root / "run" / f"preview-v{head.version}"
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "preview.mp4").write_bytes(f"fake-preview-v{head.version}".encode())
    publish_preview(episode_root, log, source_dir=f"preview-v{head.version}")
    return sha256_file(preview_dir / "preview.mp4")


def _initial_preview_ready(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, Path]:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(episode_runner, "run_real_chain", _fake_chain_factory(captured))
    body = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "rebuild executor test"},
    ).json()
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id
    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
    )
    assert exit_code == episode_runner.EXIT_SUCCESS
    assert (episode_dir / "review" / "store" / "versions.json").is_file()  # mirror ran
    assert (episode_dir / "previews" / "preview.mp4").read_bytes() == b"fake-preview-v1"
    return episode_id, episode_dir


def _apply_remove(client: TestClient, episode_id: str) -> dict[str, object]:
    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": REMOVE_TEXT, "at_seconds": None},
    )
    assert response.status_code == 200
    applied: dict[str, object] = response.json()["applied"]
    return applied


def _stage_counts(workspace: dict[str, Path], episode_id: str) -> dict[str, list[str]]:
    with StateStore.open(workspace["state_store"]) as store:
        snapshot = store.get_job_snapshot(episode_id)
    counts: dict[str, list[str]] = {}
    for run in snapshot.stage_runs:
        counts.setdefault(run.stage_name, []).append(run.status)
    return counts


def _runner_log_events(episode_dir: Path) -> list[dict[str, object]]:
    lines = (episode_dir / "runner.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


def _assert_rebuild_spawn_contract(
    runner_spawn_calls: list[dict[str, object]],
    workspace: dict[str, Path],
    episode_dir: Path,
    command_id: object,
) -> None:
    assert len(runner_spawn_calls) == 2  # intake spawn + rebuild spawn
    rebuild_argv = cast("list[str]", runner_spawn_calls[1]["argv"])
    assert rebuild_argv[:7] == [
        sys.executable,
        "-m",
        "services.cli.episode_runner",
        "--episode-root",
        str(episode_dir),
        "--stop",
        "PREVIEW_READY",
    ]
    assert rebuild_argv[rebuild_argv.index("--from-stage") + 1] == "plan"
    assert rebuild_argv[rebuild_argv.index("--applied-command") + 1] == command_id
    assert rebuild_argv[rebuild_argv.index("--state-store") + 1] == str(workspace["state_store"])
    assert runner_spawn_calls[1]["cwd"] == PIPELINE_ROOT
    assert runner_spawn_calls[1]["start_new_session"] is True


# ---------------------------------------------------------------------------
# (a) apply remove_section on a committed state → rebuild → re-entry reaches
#     PREVIEW_READY again with plan/compile/preview; unrelated stages stay.
# ---------------------------------------------------------------------------


def test_remove_section_rebuild_executes_stop_bounded_lineage(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_remove(client, episode_id)
    assert applied["command_kind"] == "remove_section"
    assert applied["result_plan_version"] == "v2"
    assert (episode_dir / "review" / "store" / "plan-v2.json").is_file()
    before_counts = _stage_counts(workspace, episode_id)
    preview_before = (episode_dir / "previews" / "preview.mp4").stat().st_mtime_ns

    response = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["scheduled"] is True
    assert body["stages"] == ["plan", "compile", "preview", "resolve_build", "qc", "render"]
    assert body["runner_log"].endswith("runner.log")
    assert body["applied_command"] == applied["command_id"]
    _assert_rebuild_spawn_contract(
        runner_spawn_calls, workspace, episode_dir, applied["command_id"]
    )

    monkeypatch.setattr(episode_runner_rebuild, "stage_preview", _fake_stage_preview)
    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        from_stage="plan",
        applied_command=str(applied["command_id"]),
    )

    assert exit_code == episode_runner.EXIT_SUCCESS
    status = client.get(f"/episodes/{episode_id}").json()
    assert status["status"] == "PREVIEW_READY"
    after_counts = _stage_counts(workspace, episode_id)
    for untouched in ("intake", "ingest", "normalize", "analyze", "selection"):
        assert after_counts[untouched] == before_counts[untouched] == ["succeeded"]
    assert after_counts["plan"] == ["succeeded", "succeeded"]
    assert after_counts["compile"] == ["succeeded"]
    assert after_counts["preview"] == ["succeeded", "succeeded"]
    assert all("running" not in runs for runs in after_counts.values())

    published = episode_dir / "previews" / "preview.mp4"
    assert published.read_bytes() == b"fake-preview-v2"  # the v2 head was consumed
    assert published.stat().st_mtime_ns > preview_before
    assert (episode_dir / "run" / "preview-v2" / "preview.mp4").is_file()

    metrics_lines = (episode_dir / "rebuild-metrics.jsonl").read_bytes().splitlines()
    assert len(metrics_lines) == 1
    metric = json.loads(metrics_lines[0])
    assert metric["applied_command"] == applied["command_id"]
    assert metric["stages"] == ["plan", "compile", "preview"]
    assert metric["rebuild_wall_clock_seconds"] >= 0.0
    assert "ingest" in metric["unrelated_stages_skipped"]
    assert "resolve_build" in metric["unrelated_stages_skipped"]
    assert metric["confirmations"] == 1

    events = _runner_log_events(episode_dir)
    kinds = [event["event"] for event in events]
    assert "rebuild_finished" in kinds
    skipped = [event for event in events if event["event"] == "rebuild_stage_skipped"]
    assert [event["stage"] for event in skipped] == ["resolve_build", "qc", "render"]


# ---------------------------------------------------------------------------
# (b) non-executable kind (lower_bgm): intent-only with the honest reason.
# ---------------------------------------------------------------------------


def test_lower_bgm_rebuild_stays_intent_only(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, _episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                      monkeypatch)
    applied = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "ここのBGMをもっと小さく", "at_seconds": 1.0},
    ).json()["applied"]
    assert applied["command_kind"] == "lower_bgm"

    response = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["scheduled"] is False
    assert body["reason"] == "command kind not rebuild-executable yet"
    assert len(runner_spawn_calls) == 1  # only the intake spawn


# ---------------------------------------------------------------------------
# (c) lease conflict: rebuild while a runner holds the episode lock → 409,
#     no second spawn; releasing the lock recovers.
# ---------------------------------------------------------------------------


def test_rebuild_conflicts_while_runner_active(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_remove(client, episode_id)
    lock_path = episode_dir / episode_ops.RUNNER_LOCK_NAME
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conflict = client.post(
            f"/episodes/{episode_id}/rebuild",
            json={"applied_command": applied["command_id"]},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "runner-active"
        assert len(runner_spawn_calls) == 1  # NO second runner spawned
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    recovered = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )
    assert recovered.status_code == 202
    assert recovered.json()["scheduled"] is True
    assert len(runner_spawn_calls) == 2


# ---------------------------------------------------------------------------
# (d) malformed re-entry arguments exit 2 with a typed log line; a not-yet-
#     preview-ready episode blocks re-entry honestly.
# ---------------------------------------------------------------------------


def test_reentry_rejects_non_reentry_and_missing_command_flags(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                      monkeypatch)

    bad_stage = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        from_stage="ingest",
        applied_command="rcmd-whatever",
    )
    assert bad_stage == episode_runner.EXIT_MALFORMED
    malformed = next(
        event for event in _runner_log_events(episode_dir) if event["event"] == "malformed"
    )
    assert malformed["code"] == "reentry-malformed"

    missing_command = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        from_stage="plan",
    )
    assert missing_command == episode_runner.EXIT_MALFORMED


def test_reentry_blocked_when_episode_not_preview_ready(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    body = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "not ready"},
    ).json()
    episode_id = str(body["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id

    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        from_stage="plan",
        applied_command="rcmd-neverapplied",
    )

    assert exit_code == episode_runner.EXIT_BLOCKED
    blocked = next(
        event for event in _runner_log_events(episode_dir) if event["event"] == "blocked"
    )
    assert blocked["code"] == "episode-not-preview-ready"


def test_reentry_at_preview_only_rerenders_latest_version(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_remove(client, episode_id)
    monkeypatch.setattr(episode_runner_rebuild, "stage_preview", _fake_stage_preview)

    exit_code = episode_runner.run(
        episode_root=episode_dir,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        from_stage="preview",
        applied_command=str(applied["command_id"]),
    )

    assert exit_code == episode_runner.EXIT_SUCCESS
    assert (episode_dir / "previews" / "preview.mp4").read_bytes() == b"fake-preview-v2"
    metric = json.loads(
        (episode_dir / "rebuild-metrics.jsonl").read_bytes().splitlines()[0]
    )
    assert metric["stages"] == ["preview"]


# ---------------------------------------------------------------------------
# (e) task-10 live Tier C catch: the REAL stage_preview bundle hand-off must
#     repoint store_dir/events_log at the cockpit review layout with
#     resolvable ../review/... paths (Path.relative_to cannot emit "..", so
#     the real rebuild preview stage crashed; the fake above had hidden it).
# ---------------------------------------------------------------------------


def test_stage_preview_repoints_bundle_at_cockpit_review_store(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                      monkeypatch)
    applied = _apply_remove(client, episode_id)
    assert applied["result_plan_version"] == "v2"
    log_path = episode_dir / "review" / "events.jsonl"
    plan_dir = episode_dir / "review" / "store"
    head = load_head(log_path, plan_dir)
    assert head.version == 2
    plan = store_plan(plan_dir / "plan-v2.json")
    ir = store_ir(plan_dir / "ir-v2.json")

    def fake_render(*_args: object, **_kwargs: object) -> None:
        preview_dir = episode_dir / "run" / "preview-v2"
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / PREVIEW_NAME).write_bytes(b"regression-preview-v2")
        (preview_dir / TRACE_NAME).write_text('{"note": "fake trace"}')

    monkeypatch.setattr(episode_runner_rebuild, "render_review_preview", fake_render)
    monkeypatch.setattr(
        episode_runner_rebuild, "previous_trace", lambda *_args: object()
    )
    monkeypatch.setattr(
        episode_runner_rebuild, "AppliedDecision", lambda **kwargs: kwargs
    )
    monkeypatch.setattr(episode_runner_rebuild, "load_tools", object)

    preview_sha = episode_runner_rebuild.stage_preview(
        episode_dir, head, plan, ir, io.BytesIO()
    )

    bundle_file = episode_dir / "run" / "review-bundle.json"
    bundle = load_bundle(bundle_file)
    assert bundle.store_dir == "../review/store"
    assert bundle.events_log == "../review/events.jsonl"
    assert (bundle_file.parent / bundle.store_dir / "plan-v2.json").is_file()
    assert (bundle_file.parent / bundle.events_log).is_file()
    assert bundle.current.plan_version == "v2"
    assert bundle.current.preview_dir == "preview-v2"
    assert preview_sha == sha256_file(
        episode_dir / "run" / "preview-v2" / PREVIEW_NAME
    )
    assert (episode_dir / "previews" / "preview.mp4").read_bytes() == b"regression-preview-v2"
