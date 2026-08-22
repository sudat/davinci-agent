"""Episode cockpit — loopback-only FastAPI over existing pipeline state (task 44)."""

from __future__ import annotations

from services.episode_cockpit.app import (
    DEFAULT_PORT,
    LOOPBACK_HOST,
    CockpitBindError,
    create_cockpit_app,
    run,
)
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitError,
    CockpitNotFoundError,
    CockpitUnprocessableError,
)

__all__ = [
    "DEFAULT_PORT",
    "LOOPBACK_HOST",
    "CockpitBindError",
    "CockpitConflictError",
    "CockpitError",
    "CockpitNotFoundError",
    "CockpitUnprocessableError",
    "CockpitWorkspace",
    "create_cockpit_app",
    "run",
]
