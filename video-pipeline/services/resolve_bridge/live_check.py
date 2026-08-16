"""Live bridge check used by QA: PASS, needs-live, or FAIL — never faked."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.connection import (
    BridgeConnectionError,
    BridgeUnavailable,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.evidence_recorder import CliRunRecord, invoked_argv, record_cli_run
from services.resolve_bridge.launch import (
    DEFAULT_TIMEOUT_SECONDS,
    OWNER_INSTRUCTIONS,
    BridgeLaunchBlocked,
    launch_and_connect,
)
from services.resolve_bridge.lifecycle import LifecycleError, run_exercise
from services.resolve_bridge.readiness import load_host_report

if TYPE_CHECKING:
    from services.resolve_bridge.models import ResolveHostReport

MARKER: Final = "todo-15-live:"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--evidence", type=Path)
    return parser


def _connect(arguments: argparse.Namespace, report: ResolveHostReport) -> ResolveConnection:
    try:
        return connect(report)
    except BridgeUnavailable as error:
        if not arguments.launch:
            raise BridgeUnavailable(
                f"Resolve unreachable and --launch not given: {error.detail}"
            ) from error
    return launch_and_connect(report, arguments.timeout)


def main() -> int:
    arguments = _parser().parse_args()
    argv = invoked_argv("services.resolve_bridge.live_check")
    lines: list[str] = []
    exit_code = 0
    try:
        report = load_host_report(arguments.report)
        connection = _connect(arguments, report)
        result = run_exercise(connection)
        binding = connection.binding
        lines.append(
            f"{MARKER} PASS product={binding.product_name} version={binding.version_core} "
            f"build={binding.build_number} project={result.project_name} "
            "only-owned-changes=true"
        )
    except BridgeLaunchBlocked as error:
        lines.append(f"{MARKER} needs-live SKIPPED reason={error.detail}")
        lines.append(error.instructions)
    except BridgeUnavailable as error:
        lines.append(f"{MARKER} needs-live SKIPPED reason={error.detail}")
        lines.append(OWNER_INSTRUCTIONS)
    except (BridgeConnectionError, LifecycleError, OSError) as error:
        lines.append(f"{MARKER} FAIL {error}")
        exit_code = 1
    stdout = "\n".join(lines) + "\n"
    print(stdout, end="")
    if arguments.evidence is not None:
        record_cli_run(
            arguments.evidence,
            CliRunRecord(
                argv=argv,
                exit_code=exit_code,
                expected_exit=exit_code,
                stdout=stdout,
                stderr="",
                observe=MARKER,
            ),
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
