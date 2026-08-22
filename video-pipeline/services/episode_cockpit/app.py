"""Loopback-only FastAPI app factory for the episode cockpit (task 44).

The cockpit is the local operator web API over EXISTING pipeline state —
never a second state machine. Loopback discipline is structural: the
factory refuses any ``bind_host`` other than ``127.0.0.1`` at creation
time, and ``run()`` passes the literal loopback constant to uvicorn, so a
non-loopback bind is a typed refusal rather than a configuration default
that could drift.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Final

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.episode_cockpit.api import router
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.errors import (
    CockpitBindError,
    CockpitConflictError,
    CockpitError,
    CockpitNotFoundError,
    CockpitUnprocessableError,
)

LOOPBACK_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8642


def _error_envelope(code: str, detail: object) -> dict[str, object]:
    return {"error": {"code": code, "detail": detail}}


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CockpitNotFoundError)
    def not_found(_request: Request, exc: CockpitNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=_error_envelope(exc.code, exc.detail))

    @app.exception_handler(CockpitConflictError)
    def conflict(_request: Request, exc: CockpitConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content=_error_envelope(exc.code, exc.detail))

    @app.exception_handler(CockpitUnprocessableError)
    def unprocessable(
        _request: Request, exc: CockpitUnprocessableError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content=_error_envelope(exc.code, exc.detail))

    @app.exception_handler(RequestValidationError)
    def invalid_request(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content=_error_envelope("validation-error", details)
        )

    @app.exception_handler(StarletteHTTPException)
    def http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = (
            "route-not-found"
            if exc.status_code == status.HTTP_404_NOT_FOUND
            else "http-error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(CockpitError)
    def cockpit_error(_request: Request, exc: CockpitError) -> JSONResponse:
        return JSONResponse(status_code=500, content=_error_envelope(exc.code, exc.detail))

    @app.exception_handler(Exception)
    def internal(_request: Request, exc: Exception) -> JSONResponse:
        traceback.print_exc(file=sys.stderr)
        return JSONResponse(
            status_code=500,
            content=_error_envelope("internal-error", type(exc).__name__),
        )


def create_cockpit_app(
    *,
    state_store_path: Path,
    episodes_root: Path,
    bind_host: str = LOOPBACK_HOST,
) -> FastAPI:
    """Build the cockpit app; refuse any non-loopback bind host at factory time."""

    if bind_host != LOOPBACK_HOST:
        raise CockpitBindError(
            "bind-not-loopback",
            f"the episode cockpit binds {LOOPBACK_HOST} only; refused {bind_host!r}",
        )
    app = FastAPI(title="episode-cockpit", version="0.1.0")
    app.state.cockpit = CockpitWorkspace(
        state_store_path=state_store_path, episodes_root=episodes_root
    )
    app.include_router(router)
    _register_error_handlers(app)
    return app


def run(
    *,
    state_store_path: Path,
    episodes_root: Path,
    host: str = LOOPBACK_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Serve the cockpit on uvicorn, loopback only (typed refusal otherwise)."""

    if host != LOOPBACK_HOST:
        raise CockpitBindError(
            "bind-not-loopback",
            f"the episode cockpit serves on {LOOPBACK_HOST} only; refused {host!r}",
        )
    app = create_cockpit_app(
        state_store_path=state_store_path, episodes_root=episodes_root, bind_host=host
    )
    uvicorn.run(app, host=LOOPBACK_HOST, port=port, log_level="info")


__all__ = [
    "DEFAULT_PORT",
    "LOOPBACK_HOST",
    "CockpitBindError",
    "create_cockpit_app",
    "run",
]
