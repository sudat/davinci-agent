"""Bounded DaVinci Resolve restart for the restart-repeatability evidence.

Quits the running app (Apple event, escalating to SIGTERM only if the polite
quit is ignored), waits for real process exit, relaunches through the bounded
launcher, and reconnects while recording before/after process identity so
evaluation can reject a restart whose binding drifted.

Live-verified process identity (Resolve Studio 21.0.4 on this host): the main
executable is ``…/DaVinci Resolve.app/Contents/MacOS/Resolve`` — the process
list is therefore read via ``ps`` with an exact executable suffix match, not a
name guess.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.connection import (
    BridgeConnectionError,
    connect,
    reset_script_module_cache,
)
from services.resolve_bridge.launch import (
    DEFAULT_TIMEOUT_SECONDS,
    BridgeLaunchBlocked,
    launch_app,
)
from services.spike.gate_models import RESTART_TIMEOUT_SECONDS, BindingSnapshot, RestartRecord

if TYPE_CHECKING:
    from services.resolve_bridge.connection import ResolveConnection
    from services.resolve_bridge.models import ResolveHostReport

APP_NAME: Final = "DaVinci Resolve"
RESOLVE_EXEC_SUFFIX: Final = "/DaVinci Resolve.app/Contents/MacOS/Resolve"
QUIT_POLL_SECONDS: Final = 2.0
KILL_GRACE_SECONDS: Final = 30.0
PID_COMM_FIELDS: Final = 2


class RestartError(Exception):
    """The bounded restart did not produce a running, bound Resolve."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _resolve_pids() -> tuple[int, ...]:
    result = subprocess.run(
        ("ps", "ax", "-o", "pid=,comm="),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == PID_COMM_FIELDS and parts[0].isdigit() and parts[1].endswith(
            RESOLVE_EXEC_SUFFIX
        ):
            pids.append(int(parts[0]))
    return tuple(pids)


def resolve_pid() -> int | None:
    pids = _resolve_pids()
    return pids[0] if pids else None


def resolve_process_running() -> bool:
    return bool(_resolve_pids())


def _binding_snapshot(connection: ResolveConnection) -> BindingSnapshot:
    binding = connection.binding
    return BindingSnapshot(
        product_name=binding.product_name,
        version_core=binding.version_core,
        build_number=binding.build_number,
        version_string=binding.version_string,
    )


def _quit_apple_event() -> None:
    subprocess.run(
        ("osascript", "-e", f'tell application "{APP_NAME}" to quit'),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _sigterm() -> None:
    for pid in _resolve_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue


def quit_and_wait(timeout_seconds: float = RESTART_TIMEOUT_SECONDS) -> tuple[str, str, str]:
    """Quit Resolve and block until the process is gone; returns (quit_at, exited_at, method)."""

    quit_at = _utc_now()
    _quit_apple_event()
    method = "apple-event-quit"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not resolve_process_running():
            return quit_at, _utc_now(), method
        time.sleep(QUIT_POLL_SECONDS)
    _sigterm()
    method = "apple-event-quit+sigterm"
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not resolve_process_running():
            return quit_at, _utc_now(), method
        time.sleep(QUIT_POLL_SECONDS)
    raise RestartError(f"Resolve did not exit within {timeout_seconds:.0f}s + SIGTERM grace")


def relaunch_and_connect(
    report: ResolveHostReport,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = 2.0,
) -> ResolveConnection:
    """Launch the app and poll ``connect`` until the bridge fully answers.

    During Resolve's startup window ``scriptapp`` may hand back an object
    whose RPC answers are still ``None``; those transient TypeErrors are
    retried inside the bounded window instead of aborting the restart.
    """

    launch_app()
    reset_script_module_cache()
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception = BridgeConnectionError("relaunch not attempted")
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        try:
            return connect(report)
        except (BridgeConnectionError, TypeError, AttributeError) as error:
            last_error = error
    detail = (
        f"scriptapp('Resolve') stayed unusable for {timeout_seconds:.0f}s after relaunch "
        f"({last_error}); 'External Scripting Using' may not be set to 'Local'"
    )
    raise BridgeLaunchBlocked(detail)


def restart_and_reconnect(
    connection: ResolveConnection,
    report: ResolveHostReport,
    relaunch_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[RestartRecord, ResolveConnection]:
    before = _binding_snapshot(connection)
    before_pid = resolve_pid()
    quit_at, exited_at, method = quit_and_wait()
    relaunch_at = _utc_now()
    error = ""
    after_connection: ResolveConnection | None = None
    try:
        after_connection = relaunch_and_connect(report, relaunch_timeout_seconds)
    except Exception as caught:  # noqa: BLE001 -- recorded verbatim as restart evidence
        error = f"{type(caught).__name__}: {caught}"
    connected_at = _utc_now()
    after = _binding_snapshot(after_connection) if after_connection is not None else before
    after_pid = resolve_pid()
    record = RestartRecord(
        before=before,
        after=after,
        before_pid=before_pid,
        after_pid=after_pid,
        quit_at=quit_at,
        exited_at=exited_at,
        relaunch_at=relaunch_at,
        connected_at=connected_at,
        quit_method=method,
        error=error,
    )
    if after_connection is None:
        raise RestartError(f"relaunch failed after restart: {error}")
    return record, after_connection
