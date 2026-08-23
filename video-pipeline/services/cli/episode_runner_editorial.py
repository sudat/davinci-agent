"""Editorial runtime mode gate for the episode runner (task 7).

Fail-fast preflight BEFORE the chain: resolve the editorial-runtime
mode (flag > ``EDITORIAL_RUNTIME_CONFIG`` env > absent), and when the
mode is ``production_model`` require the env gate plus the task-3
production provider module — blocking with
``production-model-unavailable`` (never a silent heuristic fallback).
A missing config must NOT masquerade as production: it means
``heuristic_diagnostic`` with an explicit warning line, and the
diagnostic chain env strips the director credentials so a leaked
environment can never silently turn a diagnostic run into a live one.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from services.cli.episode_runner_state import RunContext, block_stage, log_event

if TYPE_CHECKING:
    from services.job_runner.state_store import StateStore

EDITORIAL_RUNTIME_ENV: Final = "EDITORIAL_RUNTIME_CONFIG"
PRODUCTION_MODE: Final = "production_model"
DIAGNOSTIC_MODE: Final = "heuristic_diagnostic"
API_KEY_ENV: Final = "EDITORIAL_DIRECTOR_API_KEY"
NETWORK_ENV: Final = "EDITORIAL_DIRECTOR_NETWORK_ENABLED"
PRODUCTION_RUNTIME_MODULE: Final = "services.editorial_v2.model_provider"
PRODUCTION_UNAVAILABLE: Final = "production-model-unavailable"


class EditorialGateError(Exception):
    """Typed blocked-refusal carrying the operator-actionable reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def sanitized_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in (API_KEY_ENV, NETWORK_ENV)
    }


def editorial_mode(config_path: Path | None, log: BinaryIO) -> str:
    path = config_path
    if path is None:
        from_env = os.environ.get(EDITORIAL_RUNTIME_ENV)
        path = Path(from_env) if from_env else None
    if path is None:
        log_event(
            log,
            "editorial_mode",
            mode=DIAGNOSTIC_MODE,
            note="editorial-runtime config missing — diagnostic editorial mode",
        )
        return DIAGNOSTIC_MODE
    try:
        raw: object = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise EditorialGateError("editorial-runtime-unreadable", str(error)) from error
    mode = raw.get("mode") if isinstance(raw, dict) else None
    if mode in (DIAGNOSTIC_MODE, PRODUCTION_MODE):
        log_event(log, "editorial_mode", mode=mode, config=str(path))
        return str(mode)
    raise EditorialGateError(
        "editorial-runtime-mode-invalid",
        f"{path} mode must be {PRODUCTION_MODE!r} or {DIAGNOSTIC_MODE!r}, got {mode!r}",
    )


def require_production_ready(store: StateStore, ctx: RunContext) -> None:
    """Record the blocked stage and refuse BEFORE any chain work happens."""

    if not (os.environ.get(API_KEY_ENV) and os.environ.get(NETWORK_ENV) == "1"):
        raise _blocked(
            store,
            ctx,
            f"editorial runtime is {PRODUCTION_MODE} but the env gate is unset; "
            f"set {API_KEY_ENV} and {NETWORK_ENV}=1, or switch the editorial "
            f"runtime config to {DIAGNOSTIC_MODE}",
        )
    try:
        importlib.import_module(PRODUCTION_RUNTIME_MODULE)
    except ImportError as error:
        raise _blocked(
            store,
            ctx,
            f"model provider not yet available ({PRODUCTION_RUNTIME_MODULE}: "
            f"{error}); the production llm module lands with task 3",
        ) from error


def _blocked(store: StateStore, ctx: RunContext, detail: str) -> EditorialGateError:
    block_stage(store, ctx, "ingest", PRODUCTION_UNAVAILABLE)
    return EditorialGateError(PRODUCTION_UNAVAILABLE, detail)


__all__ = [
    "API_KEY_ENV",
    "DIAGNOSTIC_MODE",
    "EDITORIAL_RUNTIME_ENV",
    "NETWORK_ENV",
    "PRODUCTION_MODE",
    "PRODUCTION_RUNTIME_MODULE",
    "PRODUCTION_UNAVAILABLE",
    "EditorialGateError",
    "editorial_mode",
    "require_production_ready",
    "sanitized_env",
]
