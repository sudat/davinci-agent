"""``episode_runner`` — detached one-shot runner for one cockpit episode (task 7).

Wraps the EXISTING real chain (``run_real_chain``) without forking its
internals; ``episode_runner_state`` mirrors the chain's job-state
progression into the cockpit's job row through StateStore +
``apply_transition`` CAS (the ``real_chain._advance`` pattern) so the
StateStore stays the ONLY job-state authority. ``episode_runner_editorial``
gates the editorial runtime mode fail-fast before the chain, and
``episode_runner_workspace`` adapts the cockpit episode directory into
the chain's expected episode_root layout (no media copies; preview
linked into ``<episode-root>/previews/``).

Exit codes: 0 success, 1 blocked (typed reason in runner.log + a
``failed_blocked`` stage run through existing state semantics), 2
malformed input. If the runner dies hard, the last StateStore-recorded
stage stands (crash containment; nothing is invented post-mortem).
"""

from __future__ import annotations

import argparse
import os
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
    require_production_ready,
    sanitized_env,
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
    editorial_runtime: Path | None = None


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
    store: StateStore, ctx: RunContext, episode_root: Path, chain_env: dict[str, str]
) -> None:
    """Run the real chain under the live mirror; settle state on any outcome."""

    chain_state = episode_root / RUN_DIR_NAME / "state.sqlite3"
    stop_event = threading.Event()
    watcher = threading.Thread(
        target=watch_chain, args=(stop_event, chain_state, ctx), daemon=True
    )
    watcher.start()
    try:
        run_real_chain(episode_root, ctx.stop, episode_root / RUN_DIR_NAME, env=chain_env)
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


def _run_inner(call: RunnerInvocation, run_id: str, log: BinaryIO) -> int:
    log_event(log, "runner_started", run_id=run_id, pid=os.getpid(), stop=call.stop)
    if call.from_stage is not None:
        log_event(
            log,
            "from_stage_ignored",
            stage=call.from_stage,
            note="stage-subset re-entry lands with task 9; running the full chain",
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
        record_stage(
            store, ctx, "intake", "succeeded",
            adopted=sha256_file(call.episode_root / INTAKE_NAME),
        )
        try:
            mode = editorial_mode(call.editorial_runtime, log)
            if mode == PRODUCTION_MODE:
                require_production_ready(store, ctx)
        except EditorialGateError as error:
            raise _gate_error(error) from error
        chain_env = dict(os.environ) if mode == PRODUCTION_MODE else sanitized_env()
        try:
            video = principal_video(Path(intake.source_folder))
            write_chain_manifest(call.episode_root, ctx.job_id, video)
        except WorkspaceAdaptationError as error:
            block_stage(store, ctx, "ingest", error.code)
            raise RunnerBlockedError(error.code, error.detail) from error
        log_event(log, "chain_manifest_written", video=str(video))
        record_stage(store, ctx, "ingest", "running")
        _advance_chain(store, ctx, call.episode_root, chain_env)
        _verify_reached(store, ctx)
        if call.stop == "PREVIEW_READY":
            publish_preview(call.episode_root, log)
        log_event(log, "chain_finished", stop=call.stop)
        return EXIT_SUCCESS


def run(
    *,
    episode_root: Path,
    stop: str = "PREVIEW_READY",
    state_store_path: Path | None = None,
    from_stage: str | None = None,
    editorial_runtime: Path | None = None,
) -> int:
    """Advance one cockpit episode through the existing chain; 0/1/2."""

    call = RunnerInvocation(
        episode_root,
        stop,
        state_store_path if state_store_path is not None
        else episode_root.parent / "state.db",
        from_stage,
        editorial_runtime,
    )
    call.episode_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    with (call.episode_root / LOG_NAME).open("ab") as log:
        try:
            exit_code = _run_inner(call, run_id, log)
        except RunnerMalformedError as error:
            log_event(log, "malformed", code=error.code, detail=error.detail)
            exit_code = EXIT_MALFORMED
        except RunnerBlockedError as error:
            log_event(log, "blocked", code=error.code, detail=error.detail)
            exit_code = EXIT_BLOCKED
        except Exception:  # noqa: BLE001 (last-ditch containment; log and exit 1)
            log_event(log, "runner_crashed", detail=traceback.format_exc())
            exit_code = EXIT_BLOCKED
        log_event(log, "runner_finished", exit_code=exit_code)
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
        default=None,
        help="reserved for task 9 (partial rebuild re-entry); ignored with a log line",
    )
    parser.add_argument("--editorial-runtime", type=Path, default=None)
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
        editorial_runtime=arguments.editorial_runtime,
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
