"""``episode_runner`` — detached one-shot runner for one cockpit episode (task 7).

Wraps the EXISTING real chain (``run_real_chain``) without forking its
internals; ``episode_runner_state`` mirrors the chain's job-state
progression into the cockpit's job row through StateStore +
``apply_transition`` CAS (the ``real_chain._advance`` pattern) so the
StateStore stays the ONLY job-state authority. ``episode_runner_editorial``
gates the editorial runtime mode fail-fast before the chain, and
``episode_runner_workspace`` adapts the cockpit episode directory into
the chain's expected episode_root layout (no media copies; preview
linked into ``<episode-root>/previews/``). Task 9 adds the
``--from-stage`` stage-subset re-entry (``episode_runner_rebuild``):
plan/compile/preview against the CURRENT committed review store, then
always re-render + republish the preview.

Exit codes: 0 success, 1 blocked (typed reason in runner.log + a
``failed_blocked`` stage run through existing state semantics), 2
malformed input. If the runner dies hard, the last StateStore-recorded
stage stands (crash containment; nothing is invented post-mortem).
"""

# allow: SIZE_OK — 25x pure LOC: plan-pinned single-file orchestrator (task 7
# four-module split + task 9's mandated re-entry branch + flag surface); the
# per-stage work lives in the _state/_workspace/_rebuild satellites.

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import threading
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from pydantic import ValidationError

from services.cli.episode_runner_editorial import (
    PRODUCTION_MODE,
    EditorialGateError,
    editorial_mode,
    editorial_transport,
    require_production_ready,
    sanitized_env,
)
from services.cli.episode_runner_rebuild import (
    REENTRY_FROM_STAGES,
    RebuildStageError,
    run_reentry,
)
from services.cli.episode_runner_state import (
    STAGE_OF_STATUS,
    RunContext,
    block_running_frontier,
    block_stage,
    log_event,
    mirror_upto,
    read_chain,
    record_stage,
    watch_chain,
)
from services.cli.episode_runner_workspace import (
    RUN_DIR_NAME,
    WorkspaceAdaptationError,
    mirror_review_store,
    principal_video,
    publish_preview,
    write_chain_manifest,
)
from services.cli.real_chain import STAGE_ORDER, RealChainError, run_real_chain
from services.episode_cockpit.models import IntakeRecordV1
from services.foundation_io import sha256_file
from services.job_runner.cas import current_job_state
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import MAIN_PATH

EXIT_SUCCESS: Final = 0
EXIT_BLOCKED: Final = 1
EXIT_MALFORMED: Final = 2

LOG_NAME: Final = "runner.log"
INTAKE_NAME: Final = "intake.json"


class RunnerMalformedError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RunnerBlockedError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RunnerInvocation:
    """One parsed runner request (the CLI flag group as a value object)."""

    episode_root: Path
    stop: str
    state_store_path: Path
    from_stage: str | None = None
    applied_command: str | None = None
    editorial_runtime: Path | None = None
    run_id: str | None = None
    reservation_sequence: int | None = None
    runner_lock_fd: int | None = None


def _frame_count_from_normalize_record(episode_root: Path) -> int | None:
    """Read the already-normalized frame count if the run already exists."""

    record_path = episode_root / RUN_DIR_NAME / "normalize-record.json"
    if not record_path.is_file():
        return None
    try:
        payload = json.loads(record_path.read_bytes())
        return int(payload["drop_dup"]["expected"]["output_frames"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _ensure_decode_budget(
    episode_root: Path, log: BinaryIO, run_id: str
) -> None:
    """Scale V44_MAX_DECODE_FRAMES from the SOURCE frame count when env-unset.

    The cockpit backend boots WITHOUT the operator's V44_MAX_DECODE_FRAMES
    export, so its detached Popen child inherits 900 and typed-fails at
    analyze for the real 4K episode (8467 > 900). Scaling keeps the typed
    refusal for absurd sizes (24000 ceiling) and preserves operator
    override (env already set -> untouched). A runner.log line
    ``decode_budget_scaled`` evidences the decision.
    """

    if os.environ.get("V44_MAX_DECODE_FRAMES") is not None:
        return
    from services.cli.decode_budget import scaled_decode_budget  # noqa: PLC0415

    frame_count = _frame_count_from_normalize_record(episode_root)
    budget = scaled_decode_budget(frame_count)
    if budget is None:
        log_event(
            log,
            "decode_budget_scaled",
            run_id=run_id,
            skipped=True,
            frame_count=frame_count,
            reason="below_or_at_default",
        )
        return
    os.environ["V44_MAX_DECODE_FRAMES"] = str(budget)
    source = "normalize_record" if frame_count is not None else "fallback"
    log_event(
        log,
        "decode_budget_scaled",
        run_id=run_id,
        frame_count=frame_count,
        budget=budget,
        source=source,
    )


def _load_intake(episode_root: Path) -> IntakeRecordV1:
    try:
        return IntakeRecordV1.model_validate_json(
            (episode_root / INTAKE_NAME).read_bytes()
        )
    except OSError as error:
        raise RunnerMalformedError(
            "intake-missing", f"cannot read {episode_root / INTAKE_NAME}: {error}"
        ) from error
    except ValidationError as error:
        raise RunnerMalformedError("intake-invalid", str(error)) from error


def _gate_error(error: EditorialGateError) -> Exception:
    if error.code == "production-model-unavailable":
        return RunnerBlockedError(error.code, error.detail)
    return RunnerMalformedError(error.code, error.detail)


def _catch_up(store: StateStore, ctx: RunContext, chain_state: Path) -> None:
    final = read_chain(chain_state)
    if final is not None:
        mirror_upto(store, ctx, final[0], final[1])


def _advance_chain(
    store: StateStore,
    ctx: RunContext,
    episode_root: Path,
    chain_env: dict[str, str],
    editorial_runtime: Path | None = None,
) -> None:
    """Run the real chain under the live mirror; settle state on any outcome."""

    chain_state = episode_root / RUN_DIR_NAME / "state.sqlite3"
    stop_event = threading.Event()
    watcher = threading.Thread(
        target=watch_chain, args=(stop_event, chain_state, ctx), daemon=True
    )
    watcher.start()
    try:
        run_real_chain(
            episode_root,
            ctx.stop,
            episode_root / RUN_DIR_NAME,
            env=chain_env,
            editorial_runtime=editorial_runtime,
        )
    except RealChainError as error:
        stop_event.set()
        watcher.join(timeout=10)
        _catch_up(store, ctx, chain_state)
        block_running_frontier(store, ctx, error.code)
        raise RunnerBlockedError(error.code, str(error)) from error
    stop_event.set()
    watcher.join(timeout=10)
    _catch_up(store, ctx, chain_state)


def _verify_reached(store: StateStore, ctx: RunContext) -> None:
    reached = current_job_state(store, ctx.job_id).status
    if reached != ctx.stop:
        halted_at = STAGE_OF_STATUS.get(
            MAIN_PATH[MAIN_PATH.index(reached) + 1], "preview"
        )
        block_stage(store, ctx, halted_at, "runner-internal-error")
        raise RunnerBlockedError(
            "runner-internal-error",
            f"chain returned but the cockpit job is at {reached}, expected {ctx.stop}",
        )


def _assert_inherited_runner_lock(episode_root: Path, fd: int | None) -> None:
    """Prove this re-entry holds the episode lock inherited from the spawner.

    The spawner flocked ``<episode>/runner.lock`` and passed the descriptor
    through; the child re-asserts the same file (device + inode, regular
    file, never a symlink) and re-claims the exclusive non-blocking lock
    on the inherited description, holding it until runner exit. Missing,
    closed, mismatched, or unclaimable descriptors refuse fail-closed.
    """

    if fd is None:
        raise RunnerBlockedError(
            "runner-lock-not-held",
            "re-entry needs the inherited episode-runner lock descriptor "
            "(--runner-lock-fd); refusing without proof of the single writer",
        )
    lock_path = episode_root / "runner.lock"
    try:
        st_fd = os.fstat(fd)
        st_path = lock_path.lstat()
    except OSError as error:
        raise RunnerBlockedError(
            "runner-lock-not-held",
            f"cannot prove the inherited episode-runner lock: {error}",
        ) from error
    if (
        not stat.S_ISREG(st_fd.st_mode)
        or not stat.S_ISREG(st_path.st_mode)
        or (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino)
    ):
        raise RunnerBlockedError(
            "runner-lock-not-held",
            "the inherited lock descriptor does not name the episode "
            "runner.lock file; refusing",
        )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise RunnerBlockedError(
            "runner-lock-not-held",
            f"cannot claim the inherited episode-runner lock: {error}",
        ) from error


def _reentry_exit(store: StateStore, ctx: RunContext, call: RunnerInvocation, log: BinaryIO) -> int:
    """Validate the re-entry flags, prove the inherited lock, then rebuild."""

    if call.applied_command is None or call.from_stage not in REENTRY_FROM_STAGES:
        raise RunnerMalformedError(
            "reentry-malformed",
            f"--from-stage needs --applied-command and one of {REENTRY_FROM_STAGES}",
        )
    if call.stop != "PREVIEW_READY":
        raise RunnerMalformedError(
            "stop-unsupported-for-reentry",
            "re-entry always re-renders the preview; --stop must be PREVIEW_READY",
        )
    _assert_inherited_runner_lock(call.episode_root, call.runner_lock_fd)
    try:
        return run_reentry(store, ctx, call, log)
    except RebuildStageError as error:
        raise RunnerBlockedError(error.code, error.detail) from error


def _run_inner(call: RunnerInvocation, run_id: str, log: BinaryIO) -> int:
    log_event(
        log,
        "runner_started",
        run_id=run_id,
        pid=os.getpid(),
        stop=call.stop,
        from_stage=call.from_stage,
        applied_command=call.applied_command,
    )
    if call.stop not in STAGE_ORDER:
        raise RunnerMalformedError(
            "stop-unknown", f"--stop must be one of {STAGE_ORDER}"
        )
    intake = _load_intake(call.episode_root)
    with StateStore.open(call.state_store_path) as store:
        ctx = RunContext(call.state_store_path, intake.episode_id, run_id, call.stop, log)
        try:
            store.get_job_snapshot(ctx.job_id)
        except StateStoreError as error:
            raise RunnerMalformedError(
                "job-missing", f"no cockpit job row for {ctx.job_id}: {error}"
            ) from error
        if call.from_stage is not None:
            return _reentry_exit(store, ctx, call, log)
        record_stage(
            store, ctx, "intake", "succeeded",
            adopted=sha256_file(call.episode_root / INTAKE_NAME),
        )
        try:
            mode = editorial_mode(call.editorial_runtime, log)
            if mode == PRODUCTION_MODE:
                transport = editorial_transport(call.editorial_runtime, log)
                require_production_ready(store, ctx, transport)
        except EditorialGateError as error:
            raise _gate_error(error) from error
        _ensure_decode_budget(call.episode_root, log, run_id)
        chain_env = dict(os.environ) if mode == PRODUCTION_MODE else sanitized_env()
        if "V44_MAX_DECODE_FRAMES" in os.environ and "V44_MAX_DECODE_FRAMES" not in chain_env:
            chain_env["V44_MAX_DECODE_FRAMES"] = os.environ["V44_MAX_DECODE_FRAMES"]
        try:
            video = principal_video(Path(intake.source_folder))
            write_chain_manifest(call.episode_root, ctx.job_id, video)
        except WorkspaceAdaptationError as error:
            block_stage(store, ctx, "ingest", error.code)
            raise RunnerBlockedError(error.code, error.detail) from error
        log_event(log, "chain_manifest_written", run_id=run_id, video=str(video))
        record_stage(store, ctx, "ingest", "running")
        _advance_chain(store, ctx, call.episode_root, chain_env, call.editorial_runtime)
        _verify_reached(store, ctx)
        if call.stop == "PREVIEW_READY":
            publish_preview(call.episode_root, log, run_id=run_id)
            mirror_review_store(call.episode_root, log)
        log_event(log, "chain_finished", run_id=run_id, stop=call.stop)
        return EXIT_SUCCESS


def run(  # noqa: PLR0913 (keyword surface mirrors the argparse flag group)
    *,
    episode_root: Path,
    stop: str = "PREVIEW_READY",
    state_store_path: Path | None = None,
    from_stage: str | None = None,
    applied_command: str | None = None,
    editorial_runtime: Path | None = None,
    run_id: str | None = None,
    reservation_sequence: int | None = None,
    runner_lock_fd: int | None = None,
) -> int:
    """Advance one cockpit episode through the existing chain; 0/1/2.

    ``run_id`` (2P): when the SPAWNING parent pre-generates the run id it
    can pass it here so the rebuild-request records and the runner.log
    events name the SAME run; absent, one is generated internally.
    ``reservation_sequence`` names the consultation reservation this
    re-entry executes (selection re-entry pins to it instead of reading
    the latest policy). ``runner_lock_fd`` is the inherited episode-lock
    descriptor a re-entry must prove (fail-closed without it).
    """

    call = RunnerInvocation(
        episode_root,
        stop,
        state_store_path if state_store_path is not None
        else episode_root.parent / "state.db",
        from_stage,
        applied_command,
        editorial_runtime,
        run_id,
        reservation_sequence,
        runner_lock_fd,
    )
    call.episode_root.mkdir(parents=True, exist_ok=True)
    run_id = call.run_id if call.run_id is not None else uuid.uuid4().hex[:12]
    with (call.episode_root / LOG_NAME).open("ab") as log:
        try:
            exit_code = _run_inner(call, run_id, log)
        except RunnerMalformedError as error:
            log_event(log, "malformed", run_id=run_id, code=error.code, detail=error.detail)
            exit_code = EXIT_MALFORMED
        except RunnerBlockedError as error:
            log_event(log, "blocked", run_id=run_id, code=error.code, detail=error.detail)
            exit_code = EXIT_BLOCKED
        except Exception:  # noqa: BLE001 (last-ditch containment; log and exit 1)
            log_event(log, "runner_crashed", run_id=run_id, detail=traceback.format_exc())
            exit_code = EXIT_BLOCKED
        log_event(log, "runner_finished", run_id=run_id, exit_code=exit_code)
        return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli episode_runner",
        description="Advance one cockpit episode through the real chain (one-shot).",
    )
    parser.add_argument("--episode-root", type=Path, required=True)
    parser.add_argument("--stop", choices=STAGE_ORDER, default="PREVIEW_READY")
    parser.add_argument(
        "--from-stage",
        choices=REENTRY_FROM_STAGES,
        default=None,
        help="task-9 stage-subset re-entry: re-run [stage..preview] against the "
        "committed review store (requires --applied-command)",
    )
    parser.add_argument(
        "--applied-command",
        default=None,
        help="applied review command id the re-entry rebuilds from",
    )
    parser.add_argument("--editorial-runtime", type=Path, default=None)
    parser.add_argument(
        "--run-id",
        default=None,
        help="2P: the spawning parent's pre-generated run id (log/record linkage)",
    )
    parser.add_argument(
        "--reservation-sequence",
        type=int,
        default=None,
        help="slice2 P1: the consultation reservation sequence this "
        "selection re-entry executes (pins policy + base, never latest)",
    )
    parser.add_argument(
        "--runner-lock-fd",
        type=int,
        default=None,
        help="slice2 P1: the inherited episode-runner lock descriptor a "
        "re-entry must prove (fail-closed without it)",
    )
    parser.add_argument(
        "--state-store",
        type=Path,
        default=None,
        help="cockpit StateStore path (default: <episode-root>/../state.db)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return run(
        episode_root=arguments.episode_root,
        stop=arguments.stop,
        state_store_path=arguments.state_store,
        from_stage=arguments.from_stage,
        applied_command=arguments.applied_command,
        editorial_runtime=arguments.editorial_runtime,
        run_id=arguments.run_id,
        reservation_sequence=arguments.reservation_sequence,
        runner_lock_fd=arguments.runner_lock_fd,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_MALFORMED",
    "EXIT_SUCCESS",
    "RunnerInvocation",
    "main",
    "run",
]
