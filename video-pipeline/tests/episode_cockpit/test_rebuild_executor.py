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
from services.episode_cockpit.review_chat import (
    DEFAULT_LINEAGE,
    AppliedCommand,
    ReviewCommandDraft,
    ReviewCommandKind,
    _command_id,
    plan_rebuild,
)
from services.episode_cockpit.status_view import load_rebuild_entries
from services.foundation_io import sha256_file
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import MAIN_PATH
from services.preview.render import PREVIEW_NAME, TRACE_NAME
from services.review_command.store import HeadState, load_head

if TYPE_CHECKING:
    from typing import BinaryIO

    from services.cli.episode_runner_state import RunContext
    from services.contracts.timeline_ir import TimelineIr0C


def _locked_reentry_run(
    episode_dir: Path,
    *,
    state_store_path: Path,
    from_stage: str,
    applied_command: str,
    run_id: str | None = None,
) -> int:
    """Direct re-entry holding a real inherited-lock descriptor (P1 proof).

    Production re-entries inherit ``runner.lock`` through the spawn; direct
    test calls hold it explicitly and pass the descriptor, or the runner
    refuses fail-closed with ``runner-lock-not-held``. ``run_id`` carries
    the spawning POST's pre-generated id when given (the ``--run-id``
    production contract), so journal rows and log events name one run.
    """

    fd = os.open(episode_dir / "runner.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return episode_runner.run(
            episode_root=episode_dir,
            stop="PREVIEW_READY",
            state_store_path=state_store_path,
            from_stage=from_stage,
            applied_command=applied_command,
            runner_lock_fd=fd,
            run_id=run_id,
        )
    finally:
        os.close(fd)

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

    def fake_chain(  # noqa: PLR0913 (mirrors the run_real_chain seam)
        episode_root: Path, stop: str, out_dir: Path, *, env: dict[str, str] | None = None,
        policy_path: Path | None = None, editorial_runtime: Path | None = None,
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


def _fake_stage_preview(  # noqa: PLR0913 (mirrors the real stage_preview seam)
    episode_root: Path,
    head: HeadState,
    plan: EditPlan0C,
    ir: TimelineIr0C,
    log: BinaryIO,
    *,
    run_id: str,
    selection_attempt: object = None,
    preview_timeout_seconds: float | None = None,
    presentation: object = None,
) -> str:
    """Rebuild preview seam: version-tagged bytes, then the REAL ordering —
    bundle hand-off (_update_bundle) BEFORE publish; the publish event's
    target_version is only honest if the bundle is already repointed."""

    preview_dir = episode_root / "run" / f"preview-v{head.version}"
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "preview.mp4").write_bytes(f"fake-preview-v{head.version}".encode())
    (preview_dir / TRACE_NAME).write_text('{"note": "fake trace"}')
    preview_sha = sha256_file(preview_dir / "preview.mp4")
    bundle_file = episode_root / "run" / "review-bundle.json"
    episode_runner_rebuild._update_bundle(bundle_file, load_bundle(bundle_file), head, preview_sha)
    publish_preview(episode_root, log, source_dir=f"preview-v{head.version}", run_id=run_id)
    return preview_sha


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
    # Adoption authority is the server-saved proposal set: preview, then apply.
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": REMOVE_TEXT, "at_seconds": None},
    )
    assert preview.status_code == 200
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


def _assert_publish_events(
    episode_dir: Path, events: list[dict[str, object]]
) -> None:
    """Both publishes carry run/version/hash; each hash matches its artifact."""

    started = [event for event in events if event["event"] == "runner_started"]
    published = [event for event in events if event["event"] == "preview_published"]
    assert len(published) == 2
    assert published[0]["run_id"] == started[0]["run_id"]
    assert published[0]["target_version"] == "v1"
    assert published[0]["content_hash"] == sha256_file(
        episode_dir / "run" / "preview-v1" / "preview.mp4"
    )
    assert published[1]["run_id"] == started[-1]["run_id"]
    assert published[1]["target_version"] == "v2"
    assert published[1]["content_hash"] == sha256_file(
        episode_dir / "previews" / "preview.mp4"
    )


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
    exit_code = _locked_reentry_run(
        episode_dir,
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
    _assert_publish_events(episode_dir, events)


# ---------------------------------------------------------------------------
# (a3) metrics-before-completion invariant (live-lane ordering fix): the
#      rebuild-metrics.jsonl record must be durably written BEFORE the
#      terminal preview ``succeeded`` row — the state transition behind the
#      observable 再build完了. A status poll landing between those two
#      writes would otherwise observe 完了 with no recorded evidence.
# ---------------------------------------------------------------------------


def test_rebuild_metrics_written_before_terminal_preview_success(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INVARIANT metrics-before-completion: metrics file precedes 完了 state.

    When the terminal preview ``succeeded`` stage row is recorded, the
    rebuild-metrics.jsonl record for this run's applied command must
    already exist. Fails on the old write order (metrics appended after
    the terminal row); passes with the deferred-terminal-success order
    in ``run_reentry``.
    """
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_remove(client, episode_id)
    command_id = str(applied["command_id"])
    monkeypatch.setattr(episode_runner_rebuild, "stage_preview", _fake_stage_preview)
    real_record_stage = episode_runner_rebuild.record_stage
    terminal_checks: list[bool] = []
    # Collected (not raised) inside the spy: episode_runner.run swallows
    # any stage-recording exception into runner_crashed/EXIT_BLOCKED, so a
    # raise here would surface only as an exit-code mismatch. Assert after.
    violations: list[str] = []

    def spying_record_stage(
        store: StateStore, ctx: RunContext, stage: str, status: str, **kwargs: object
    ) -> None:
        if stage == "preview" and status == "succeeded":
            terminal_checks.append(True)
            try:
                raw_lines = (episode_dir / "rebuild-metrics.jsonl").read_bytes().splitlines()
            except OSError:
                raw_lines = []
            if not any(
                json.loads(line).get("applied_command") == command_id
                for line in raw_lines if line.strip()
            ):
                violations.append(command_id)
        real_record_stage(store, ctx, stage, status, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(episode_runner_rebuild, "record_stage", spying_record_stage)
    exit_code = _locked_reentry_run(
        episode_dir,
        state_store_path=workspace["state_store"],
        from_stage="plan",
        applied_command=command_id,
    )

    assert exit_code == episode_runner.EXIT_SUCCESS
    assert terminal_checks, "the terminal preview succeeded row was never recorded"
    assert not violations, (
        "INVARIANT metrics-before-completion violated: the terminal "
        "preview succeeded row was recorded before rebuild-metrics.jsonl "
        f"contained {command_id}"
    )
    metric_lines = (episode_dir / "rebuild-metrics.jsonl").read_bytes().splitlines()
    assert len(metric_lines) == 1
    assert json.loads(metric_lines[0])["applied_command"] == command_id
    with StateStore.open(workspace["state_store"]) as store:
        preview_rows = [
            run for run in store.get_job_snapshot(episode_id).stage_runs
            if run.stage_name == "preview" and run.status == "succeeded"
        ]
    assert len(preview_rows) == 2
    assert all(run.first_output_arrived_at is not None for run in preview_rows)


# ---------------------------------------------------------------------------
# (a2) multi-command apply (V44-1): echoed drafts → 2 AppliedCommands +
#      ONE union rebuild scheduled + the re-entry consumes the latest head.
# ---------------------------------------------------------------------------


def _echo_draft(
    kind: ReviewCommandKind, target: float, text: str, *, delta: float | None = None
) -> ReviewCommandDraft:
    return ReviewCommandDraft.model_validate(
        {
            "command_id": _command_id(kind, target, delta, text),
            "command_kind": kind,
            "text": text,
            "target_seconds": target,
            "seconds_delta": delta,
            "scope": "episode",
            "needs_confirmation": False,
            "confirmation_reason": None,
        }
    )


def test_multi_draft_apply_two_commands_one_union_rebuild(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)

    def fake_llm(text: str, context: object, nearby: object) -> dict:
        del text, context, nearby
        return {
            "proposals": [
                {"command_kind": "remove_section", "target_seconds": 0.5},
                {"command_kind": "keep_longer", "target_seconds": 1.0, "seconds_delta": 2.0},
            ]
        }

    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake_llm)
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": "冒頭のあいさスを削除して、あとのところは2秒長く残して"},
    )
    assert preview.status_code == 200
    previewed = preview.json()["drafts"]
    assert len(previewed) == 2

    applied_response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={
            "text": "冒頭のあいさスを削除して、あとのところは2秒長く残して",
            "at_seconds": None,
            "drafts": previewed,
        },
    )
    assert applied_response.status_code == 200
    body = applied_response.json()
    assert [a["target_seconds"] for a in body["applied_commands"]] == [0.5, 1.0]
    assert body["applied"]["command_id"] == body["applied_commands"][0]["command_id"]
    versions = [a["result_plan_version"] for a in body["applied_commands"]]
    assert versions == ["v2", "v3"]  # each command its own sealed plan version
    union_stages = ["plan", "compile", "preview", "resolve_build", "qc", "render"]
    assert list(body["rebuild"]["stages"]) == union_stages  # edit_plan lineage union

    rebuilt = client.post(
        f"/episodes/{episode_id}/rebuild",
        json={"applied_commands": [a["command_id"] for a in body["applied_commands"]]},
    )
    assert rebuilt.status_code == 202
    rebuild_body = rebuilt.json()
    assert rebuild_body["scheduled"] is True
    assert rebuild_body["stages"] == union_stages
    assert rebuild_body["applied_command"] == body["applied_commands"][0]["command_id"]
    assert rebuild_body["applied_commands"] == [
        a["command_id"] for a in body["applied_commands"]
    ]
    assert len(runner_spawn_calls) == 2  # intake spawn + ONE rebuild spawn
    rebuild_argv = cast("list[str]", runner_spawn_calls[1]["argv"])
    assert rebuild_argv[rebuild_argv.index("--from-stage") + 1] == "plan"
    assert (
        rebuild_argv[rebuild_argv.index("--applied-command") + 1]
        == body["applied_commands"][0]["command_id"]
    )

    monkeypatch.setattr(episode_runner_rebuild, "stage_preview", _fake_stage_preview)
    exit_code = _locked_reentry_run(
        episode_dir,
        state_store_path=workspace["state_store"],
        from_stage="plan",
        applied_command=str(body["applied_commands"][0]["command_id"]),
    )
    assert exit_code == episode_runner.EXIT_SUCCESS
    assert (episode_dir / "previews" / "preview.mp4").read_bytes() == b"fake-preview-v3"


def test_multi_draft_apply_rejects_forged_command_id(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, _episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                      monkeypatch)
    text = "0:00と0:02のあいさりとテストのところを削除して"
    preview = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": text, "at_seconds": None}
    )
    assert preview.status_code == 200
    forged = dict(preview.json()["draft"])
    forged["target_seconds"] = 1.0  # differs from the saved proposal set

    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": text, "at_seconds": None, "drafts": [forged]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "proposal-mismatch"
    assert "draft-not-confirmed" not in response.text


# ---------------------------------------------------------------------------
# (b) non-executable kind (use_other_take, selection domain): intent-only
#     with the honest reason. Presentation kinds used to live here
#     (lower_bgm); since the presentation-domain wiring they schedule a
#     compile-first rebuild like edit_plan kinds (see (f) below).
# ---------------------------------------------------------------------------


def test_selection_kind_rebuild_stays_intent_only(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, _episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                      monkeypatch)
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": "別のテイクを使って", "at_seconds": 1.0},
    )
    assert preview.status_code == 200
    applied = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "別のテイクを使って", "at_seconds": 1.0},
    ).json()["applied"]
    assert applied["command_kind"] == "use_other_take"
    assert applied["affected_domain"] == "selection"

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

    exit_code = _locked_reentry_run(
        episode_dir,
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

    exit_code = _locked_reentry_run(
        episode_dir,
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

    log = io.BytesIO()
    preview_sha = episode_runner_rebuild.stage_preview(
        episode_dir, head, plan, ir, log, run_id="run-binding01"
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
    published = next(
        event
        for event in (
            json.loads(line) for line in log.getvalue().splitlines() if line.startswith(b"{")
        )
        if event["event"] == "preview_published"
    )
    assert published["run_id"] == "run-binding01"
    assert published["target_version"] == "v2"
    assert published["content_hash"] == sha256_file(
        episode_dir / "previews" / "preview.mp4"
    )


# ---------------------------------------------------------------------------
# (f) presentation-domain kind (subtitle_shorter, the r9c gap): the applied
#     command schedules a compile-first rebuild through the SAME
#     reservation/spawn machinery as edit_plan kinds — the committed plan is
#     unchanged (intent-only apply), so the re-entry re-derives the output
#     from compile without selection.
# ---------------------------------------------------------------------------

SUBTITLE_TEXT = "字幕を短くして見やすくして"


def _apply_subtitle_shorter(client: TestClient, episode_id: str) -> dict[str, object]:
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": SUBTITLE_TEXT, "at_seconds": None},
    )
    assert preview.status_code == 200
    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": SUBTITLE_TEXT, "at_seconds": None},
    )
    assert response.status_code == 200
    applied: dict[str, object] = response.json()["applied"]
    return applied


def test_subtitle_shorter_rebuild_schedules_compile_lineage(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_subtitle_shorter(client, episode_id)
    assert applied["command_kind"] == "subtitle_shorter"
    assert applied["affected_domain"] == "presentation"
    assert applied["event_id"] is None
    assert applied["result_plan_version"] is None  # intent-only: plan unchanged

    response = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["scheduled"] is True
    assert body["stages"] == ["compile", "preview", "resolve_build", "qc", "render"]
    assert body["runner_log"].endswith("runner.log")
    assert body["applied_command"] == applied["command_id"]
    assert len(runner_spawn_calls) == 2  # intake spawn + rebuild spawn
    rebuild_argv = cast("list[str]", runner_spawn_calls[1]["argv"])
    assert rebuild_argv[rebuild_argv.index("--from-stage") + 1] == "compile"
    assert rebuild_argv[rebuild_argv.index("--applied-command") + 1] == applied["command_id"]
    assert rebuild_argv[rebuild_argv.index("--state-store") + 1] == str(
        workspace["state_store"]
    )
    assert "--reservation-sequence" not in rebuild_argv  # no selection budget touch

    entries = load_rebuild_entries(episode_dir)
    assert len(entries) == 2
    reservation, spawned = entries
    assert reservation.spawned is False
    assert reservation.run_id is None
    assert spawned.spawned is True
    assert spawned.reserves_sequence == reservation.sequence
    assert spawned.run_id == body["run_id"]
    assert spawned.target_version is None  # intent-only: no new plan version


def test_subtitle_shorter_reentry_executes_compile_and_preview_only(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_subtitle_shorter(client, episode_id)
    rebuild = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )
    assert rebuild.json()["scheduled"] is True
    rebuild_run_id = str(rebuild.json()["run_id"])
    before_counts = _stage_counts(workspace, episode_id)
    preview_before = (episode_dir / "previews" / "preview.mp4").stat().st_mtime_ns

    monkeypatch.setattr(episode_runner_rebuild, "stage_preview", _fake_stage_preview)
    exit_code = _locked_reentry_run(
        episode_dir,
        state_store_path=workspace["state_store"],
        from_stage="compile",
        applied_command=str(applied["command_id"]),
        run_id=rebuild_run_id,
    )

    assert exit_code == episode_runner.EXIT_SUCCESS
    status = client.get(f"/episodes/{episode_id}").json()
    assert status["status"] == "PREVIEW_READY"
    after_counts = _stage_counts(workspace, episode_id)
    for untouched in ("intake", "ingest", "normalize", "analyze", "selection", "plan"):
        assert after_counts[untouched] == before_counts[untouched] == ["succeeded"]
    # The chain mirror never writes a compile row, so the re-entry owns the
    # only one — pinned to the rebuild run; preview gains its second row.
    assert after_counts["compile"] == ["succeeded"]
    assert after_counts["preview"] == ["succeeded", "succeeded"]
    with StateStore.open(workspace["state_store"]) as store:
        compile_rows = [
            run
            for run in store.get_job_snapshot(episode_id).stage_runs
            if run.stage_name == "compile"
        ]
    assert [run.run_id for run in compile_rows] == [rebuild_run_id]

    published = episode_dir / "previews" / "preview.mp4"
    assert published.read_bytes() == b"fake-preview-v1"  # same head re-rendered
    assert published.stat().st_mtime_ns > preview_before

    metric_lines = (episode_dir / "rebuild-metrics.jsonl").read_bytes().splitlines()
    assert len(metric_lines) == 1
    metric = json.loads(metric_lines[0])
    assert metric["applied_command"] == applied["command_id"]
    assert metric["stages"] == ["compile", "preview"]
    assert "selection" in metric["unrelated_stages_skipped"]
    assert "plan" in metric["unrelated_stages_skipped"]
    assert "resolve_build" in metric["unrelated_stages_skipped"]

    events = _runner_log_events(episode_dir)
    kinds = [event["event"] for event in events]
    assert "rebuild_finished" in kinds
    skipped = [event for event in events if event["event"] == "rebuild_stage_skipped"]
    assert [event["stage"] for event in skipped] == ["resolve_build", "qc", "render"]


# ---------------------------------------------------------------------------
# (f2) presentation re-request parity: the applied-command path keeps its
#      append-only resend semantics (a fresh reservation + spawn, never a
#      rewrite of the first chain) — exactly like edit_plan re-requests.
# ---------------------------------------------------------------------------


def test_presentation_rebuild_rerequest_spawns_fresh_run(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _initial_preview_ready(client, workspace, source_folder,
                                                     monkeypatch)
    applied = _apply_subtitle_shorter(client, episode_id)

    first = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )
    second = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["scheduled"] is True
    assert second.json()["scheduled"] is True
    assert second.json()["run_id"] != first.json()["run_id"]
    assert len(runner_spawn_calls) == 3  # intake spawn + two rebuild spawns
    entries = load_rebuild_entries(episode_dir)
    assert len(entries) == 4
    assert [entry.spawned for entry in entries] == [False, True, False, True]
    assert entries[1].reserves_sequence == entries[0].sequence
    assert entries[3].reserves_sequence == entries[2].sequence
    assert entries[3].run_id == second.json()["run_id"]


# ---------------------------------------------------------------------------
# (f3) lineage derivation: every presentation kind feeds the compile-first
#      stage set (hermetic — no episode needed, the derivation is pure).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["subtitle_shorter", "remove_effect", "lower_bgm", "match_color", "channel_lower_third"],
)
def test_presentation_kinds_derive_compile_first_lineage(kind: ReviewCommandKind) -> None:
    applied = AppliedCommand(
        command_id="rcmd-presentation-lineage",
        command_kind=kind,
        affected_domain="presentation",
    )
    plan = plan_rebuild(applied, DEFAULT_LINEAGE)
    assert list(plan.stages) == ["compile", "preview", "resolve_build", "qc", "render"]
    assert "selection" in plan.excluded_stages
    assert "plan" in plan.excluded_stages


# ---------------------------------------------------------------------------
# (f4) gates unchanged: a compile-first re-entry on a not-yet-preview-ready
#      episode blocks honestly through the shared runner gate.
# ---------------------------------------------------------------------------


def test_presentation_reentry_blocked_when_episode_not_preview_ready(
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
    episode_dir = workspace["episodes_root"] / str(body["episode_id"])

    exit_code = _locked_reentry_run(
        episode_dir,
        state_store_path=workspace["state_store"],
        from_stage="compile",
        applied_command="rcmd-neverapplied",
    )

    assert exit_code == episode_runner.EXIT_BLOCKED
    blocked = next(
        event for event in _runner_log_events(episode_dir) if event["event"] == "blocked"
    )
    assert blocked["code"] == "episode-not-preview-ready"
