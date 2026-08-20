"""LIVE preview/final parity driver CLI (Todo 61).

One Presentation Manifest in, two bridge renders out (preview geometry,
final geometry), one measured parity verdict. The comparator never
asserts container byte equality; every number in the report is measured
from the rendered bytes under the declared tolerances. Every run deletes
only the owned ``__fvp_test__`` project and always leaves Resolve running.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from services.presentation.parity_live_flow import ParityLiveFlow
from services.presentation.parity_live_media import MARKER, ParityLiveError
from services.resolve_bridge.connection import BridgeConnectionError, connect
from services.resolve_bridge.lifecycle import cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report

EXIT_UNAVAILABLE: Final = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    bundle = arguments.evidence / "parity-live"
    bundle.mkdir(parents=True, exist_ok=True)
    try:
        report = load_host_report(arguments.report)
        connection = connect(report)
    except (BridgeConnectionError, OSError) as error:
        print(f"{MARKER} bridge unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    flow = ParityLiveFlow(
        connection=connection,
        bundle=bundle,
        ffmpeg=arguments.ffmpeg,
        ffprobe=arguments.ffprobe,
        render_deadline=arguments.timeout,
    )
    try:
        flow.run()
    except (ParityLiveError, OSError, ValueError) as error:
        print(f"{MARKER} FAIL {error}", file=sys.stderr)
    finally:
        try:
            cleanup_owned_projects(connection.project_manager())
        except Exception as error:  # noqa: BLE001 (cleanup must not mask failures)
            print(f"{MARKER} cleanup warning: {error}", file=sys.stderr)
    print(f"{MARKER} {'PASS' if flow.passed else 'FAIL'}")
    return 0 if flow.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
