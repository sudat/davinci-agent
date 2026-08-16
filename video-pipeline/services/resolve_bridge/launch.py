"""Bounded local launcher for DaVinci Resolve with owner-only unblock instructions.

Launches the registered app, polls the official bridge for a bounded window,
and on refusal reports a BLOCKED reason plus exact owner instructions. It never
mutates Resolve preferences or dismisses dialogs itself.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.connection import (
    BridgeConnectionError,
    BridgeUnavailable,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.readiness import PREFERENCES, load_host_report

if TYPE_CHECKING:
    from services.resolve_bridge.models import ResolveHostReport

APP_NAME: Final = "DaVinci Resolve"
PROCESS_MATCH: Final = "DaVinci Resolve.app/Contents/MacOS/DaVinci Resolve"
DEFAULT_TIMEOUT_SECONDS: Final = 240.0
POLL_SECONDS: Final = 3.0
PREFERENCE_MARKERS: Final = (b"ExternalScriptingUsing", b"externalScripting", b"External Scripting")
EXIT_BLOCKED: Final = 3

OWNER_INSTRUCTIONS: Final = (
    "Owner action required (this tool never modifies Resolve settings):\n"
    "  1. Open DaVinci Resolve and dismiss any activation/license dialog if one is shown.\n"
    "  2. Open Preferences -> System -> General.\n"
    "  3. Set 'External Scripting Using' to 'Local'.\n"
    "  4. Close Preferences and rerun this command."
)


class BridgeLaunchBlocked(BridgeConnectionError):
    """The bounded launch window expired without a scripting connection."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.instructions = OWNER_INSTRUCTIONS


def resolve_process_running() -> bool:
    result = subprocess.run(
        ("pgrep", "-f", PROCESS_MATCH),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def preference_posture() -> str:
    markers = ", ".join(marker.decode() for marker in PREFERENCE_MARKERS)
    if not PREFERENCES.is_dir():
        return f"no external-scripting preference marker found (looked for: {markers})"
    for path in sorted(PREFERENCES.iterdir()):
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if any(marker in payload for marker in PREFERENCE_MARKERS):
            return f"external-scripting marker present in {path.name}"
    return f"no external-scripting preference marker found (looked for: {markers})"


def launch_app() -> None:
    subprocess.run(("open", "-a", APP_NAME), check=True, capture_output=True, text=True)


def poll_until_connected(
    attempt: Callable[[], ResolveConnection],
    timeout_seconds: float,
    poll_seconds: float,
) -> ResolveConnection:
    launch_app()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        try:
            return attempt()
        except BridgeUnavailable:
            continue
    if not resolve_process_running():
        detail = (
            "DaVinci Resolve is not running after launch; an activation or license "
            "dialog may be blocking startup"
        )
    else:
        detail = (
            f"scriptapp('Resolve') stayed unreachable for {timeout_seconds:.0f}s while "
            f"DaVinci Resolve was running; {preference_posture()}; 'External Scripting "
            "Using' is most likely not set to 'Local'"
        )
    raise BridgeLaunchBlocked(detail)


def launch_and_connect(
    report: ResolveHostReport,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> ResolveConnection:
    def attempt() -> ResolveConnection:
        return connect(report)
    try:
        return attempt()
    except BridgeUnavailable:
        return poll_until_connected(attempt, timeout_seconds, poll_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        report = load_host_report(arguments.report)
        connection = launch_and_connect(report, arguments.timeout)
    except BridgeLaunchBlocked as error:
        print(f"BLOCKED: {error.detail}", file=sys.stderr)
        print(error.instructions, file=sys.stderr)
        return EXIT_BLOCKED
    except (BridgeConnectionError, OSError) as error:
        print(f"launch failed: {error}", file=sys.stderr)
        return 2
    binding = connection.binding
    print(
        f"connected: {binding.product_name} {binding.version_core} "
        f"build {binding.build_number} ({binding.version_string})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
