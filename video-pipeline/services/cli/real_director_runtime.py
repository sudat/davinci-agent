"""Director runtime route resolution for the real-episode chain.

Which director path serves is decided by the resolved editorial runtime —
explicit ``runtime_path`` arg > ``EDITORIAL_RUNTIME_CONFIG`` env > repo
default — never by the credential env alone, and never silently mixed.
This module owns the resolution only; the director seam itself
(``real_director``) and the rebuild gate (``episode_runner_rebuild``) share
it so the initial chain and the adopted-policy rerun always agree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from services.editorial.transport import CREDENTIALS_ENV

# Contract note: SAME names as episode_runner_editorial, mirrored (not
# imported) — episode_runner_editorial reaches back through
# episode_runner_state into real_chain, so importing it here would cycle.
# Keep in sync.
EDITORIAL_RUNTIME_ENV: Final = "EDITORIAL_RUNTIME_CONFIG"
PRODUCTION_MODE: Final = "production_model"
DIAGNOSTIC_MODE: Final = "heuristic_diagnostic"
CODEX_TRANSPORT: Final = "codex-exec"
OPENAI_TRANSPORT: Final = "openai-api"
_REPO_DEFAULT_RUNTIME: Final = Path("config") / "editorial-runtime.json"

type DirectorMode = Literal["deterministic-baseline", "live"]
type DirectorTransport = Literal["deterministic-baseline", "codex-exec", "openai-api"]


class RealDirectorError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DirectorRoute:
    """Which director path serves: the resolved runtime decides, never mixed."""

    mode: DirectorMode
    transport: DirectorTransport


def _runtime_config_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_runtime_raw(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise RealDirectorError(
            "editorial-runtime-unreadable",
            f"cannot read editorial runtime at {path}: {error}",
        ) from error


def resolve_director_route(
    env: dict[str, str], runtime_path: Path | None = None
) -> DirectorRoute:
    """Resolve the director path from the editorial runtime (transport decides).

    Precedence mirrors ``episode_runner_editorial``/the consultation factory:
    explicit ``runtime_path`` > ``EDITORIAL_RUNTIME_CONFIG`` env > repo
    default. No config anywhere means the diagnostic lane
    (deterministic-baseline). ``production_model`` + ``codex-exec`` is the
    flat-rate live path (the key env is ignored, never consulted);
    ``production_model`` + ``openai-api`` is live only with the credential
    env present, exactly as before. Anything malformed is a typed refusal —
    never a silent baseline.
    """

    if runtime_path is None:
        from_env = env.get(EDITORIAL_RUNTIME_ENV)
        candidate = Path(from_env) if from_env else None
    else:
        candidate = runtime_path
    if candidate is None:
        default = _runtime_config_root() / _REPO_DEFAULT_RUNTIME
        candidate = default if default.is_file() else None
    if candidate is None:
        return DirectorRoute(
            mode="deterministic-baseline", transport="deterministic-baseline"
        )
    raw = _read_runtime_raw(candidate)
    mode = raw.get("mode") if isinstance(raw, dict) else None
    if mode == DIAGNOSTIC_MODE:
        return DirectorRoute(
            mode="deterministic-baseline", transport="deterministic-baseline"
        )
    if mode != PRODUCTION_MODE:
        raise RealDirectorError(
            "editorial-runtime-mode-invalid",
            f"{candidate} mode must be {PRODUCTION_MODE!r} or {DIAGNOSTIC_MODE!r}, "
            f"got {mode!r}",
        )
    transport = raw.get("transport") if isinstance(raw, dict) else None
    if transport is None:
        transport = CODEX_TRANSPORT
    if transport == CODEX_TRANSPORT:
        return DirectorRoute(mode="live", transport="codex-exec")
    if transport == OPENAI_TRANSPORT:
        if env.get(CREDENTIALS_ENV):
            return DirectorRoute(mode="live", transport="openai-api")
        return DirectorRoute(
            mode="deterministic-baseline", transport="deterministic-baseline"
        )
    raise RealDirectorError(
        "editorial-runtime-transport-invalid",
        f"editorial runtime transport must be one of "
        f"{(CODEX_TRANSPORT, OPENAI_TRANSPORT)}, got {transport!r}",
    )


__all__ = [
    "CODEX_TRANSPORT",
    "DIAGNOSTIC_MODE",
    "EDITORIAL_RUNTIME_ENV",
    "OPENAI_TRANSPORT",
    "PRODUCTION_MODE",
    "DirectorMode",
    "DirectorRoute",
    "DirectorTransport",
    "RealDirectorError",
    "resolve_director_route",
]
