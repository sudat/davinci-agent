"""Editorial runtime mode gate for the episode runner (task 7).

Fail-fast preflight BEFORE the chain: resolve the editorial-runtime
mode + transport (flag > ``EDITORIAL_RUNTIME_CONFIG`` env > absent), and
when the mode is ``production_model`` require the transport's own gate —
``openai-api`` needs the env credentials, ``codex-exec`` (the DEFAULT per
the owner decision, v4.4 delta) needs the codex CLI installed and logged
in — plus the task-3 production provider module, blocking with
``production-model-unavailable`` (never a silent heuristic fallback).
A missing config must NOT masquerade as production: it means
``heuristic_diagnostic`` with an explicit warning line, and the
diagnostic chain env strips the director + moment-review provider
credentials so a leaked environment can never silently turn a
diagnostic run into a live one.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from services.cli.episode_runner_state import RunContext, block_stage, log_event
from services.cli.live_editorial_codex import CodexTransportGatedError, make_codex_runner

if TYPE_CHECKING:
    from services.job_runner.state_store import StateStore

EDITORIAL_RUNTIME_ENV: Final = "EDITORIAL_RUNTIME_CONFIG"
PRODUCTION_MODE: Final = "production_model"
DIAGNOSTIC_MODE: Final = "heuristic_diagnostic"
CODEX_TRANSPORT: Final = "codex-exec"
OPENAI_TRANSPORT: Final = "openai-api"
VALID_TRANSPORTS: Final = (CODEX_TRANSPORT, OPENAI_TRANSPORT)
API_KEY_ENV: Final = "EDITORIAL_DIRECTOR_API_KEY"
NETWORK_ENV: Final = "EDITORIAL_DIRECTOR_NETWORK_ENABLED"
# Moment-review provider credentials (audit §7 hardening): the diagnostic
# chain never constructs the Gemini/GLM providers, but the guarantee must
# rest on stripping — not on that absence — so a leaked key can never turn
# a diagnostic run live through a future provider wiring.
GEMINI_KEY_ENV: Final = "GEMINI_API_KEY"
GEMINI_NETWORK_ENV: Final = "GEMINI_NETWORK_ENABLED"
ZAI_KEY_ENV: Final = "ZAI_API_KEY"
ZAI_NETWORK_ENV: Final = "ZAI_NETWORK_ENABLED"
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
        if key
        not in (
            API_KEY_ENV,
            NETWORK_ENV,
            GEMINI_KEY_ENV,
            GEMINI_NETWORK_ENV,
            ZAI_KEY_ENV,
            ZAI_NETWORK_ENV,
        )
    }


def _resolve_config_path(config_path: Path | None) -> Path | None:
    if config_path is not None:
        return config_path
    from_env = os.environ.get(EDITORIAL_RUNTIME_ENV)
    return Path(from_env) if from_env else None


def _read_runtime_raw(config_path: Path | None) -> object | None:
    path = _resolve_config_path(config_path)
    if path is None:
        return None
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise EditorialGateError("editorial-runtime-unreadable", str(error)) from error


def editorial_mode(config_path: Path | None, log: BinaryIO) -> str:
    path = _resolve_config_path(config_path)
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


def editorial_transport(config_path: Path | None, log: BinaryIO) -> str:
    """Resolve the production transport (absent field → codex-exec default).

    The transport is recorded in the runner log (``editorial_transport``
    event) so a run's reports always surface WHICH transport carried the
    editorial calls.
    """

    raw = _read_runtime_raw(config_path)
    transport = raw.get("transport") if isinstance(raw, dict) else None
    if transport is None:
        transport = CODEX_TRANSPORT
    if transport not in VALID_TRANSPORTS:
        raise EditorialGateError(
            "editorial-runtime-transport-invalid",
            f"editorial runtime transport must be one of {VALID_TRANSPORTS}, got {transport!r}",
        )
    log_event(log, "editorial_transport", transport=transport)
    return str(transport)


def require_production_ready(
    store: StateStore, ctx: RunContext, transport: str = CODEX_TRANSPORT
) -> None:
    """Record the blocked stage and refuse BEFORE any chain work happens.

    ``openai-api`` keeps the env-var gate; ``codex-exec`` gates on the codex
    CLI probe (binary + logged in, evaluated per call — never cached). Both
    block typed ``production-model-unavailable`` with an operator-actionable
    message; neither ever falls back to the heuristic planner silently.
    """

    try:
        importlib.import_module(PRODUCTION_RUNTIME_MODULE)
    except ImportError as error:
        raise _blocked(
            store,
            ctx,
            f"model provider not yet available ({PRODUCTION_RUNTIME_MODULE}: "
            f"{error}); the production llm module lands with task 3",
        ) from error
    if transport == CODEX_TRANSPORT:
        try:
            make_codex_runner()
        except CodexTransportGatedError as error:
            raise _blocked(
                store,
                ctx,
                f"editorial runtime is {PRODUCTION_MODE} with transport codex-exec "
                f"but the codex gate failed ({error.code}: {error.detail}); "
                "run `codex login` (and put codex-cli on PATH), or switch the "
                "editorial runtime config transport to openai-api "
                f"({API_KEY_ENV} + {NETWORK_ENV}=1) or mode to {DIAGNOSTIC_MODE}",
            ) from error
        return
    if not (os.environ.get(API_KEY_ENV) and os.environ.get(NETWORK_ENV) == "1"):
        raise _blocked(
            store,
            ctx,
            f"editorial runtime is {PRODUCTION_MODE} with transport openai-api but "
            f"the env gate is unset; set {API_KEY_ENV} and {NETWORK_ENV}=1, or "
            f"switch the editorial runtime config to {DIAGNOSTIC_MODE}",
        )


def _blocked(store: StateStore, ctx: RunContext, detail: str) -> EditorialGateError:
    block_stage(store, ctx, "ingest", PRODUCTION_UNAVAILABLE)
    return EditorialGateError(PRODUCTION_UNAVAILABLE, detail)


__all__ = [
    "API_KEY_ENV",
    "CODEX_TRANSPORT",
    "DIAGNOSTIC_MODE",
    "EDITORIAL_RUNTIME_ENV",
    "GEMINI_KEY_ENV",
    "GEMINI_NETWORK_ENV",
    "NETWORK_ENV",
    "OPENAI_TRANSPORT",
    "PRODUCTION_MODE",
    "PRODUCTION_RUNTIME_MODULE",
    "PRODUCTION_UNAVAILABLE",
    "VALID_TRANSPORTS",
    "ZAI_KEY_ENV",
    "ZAI_NETWORK_ENV",
    "EditorialGateError",
    "editorial_mode",
    "editorial_transport",
    "require_production_ready",
    "sanitized_env",
]
