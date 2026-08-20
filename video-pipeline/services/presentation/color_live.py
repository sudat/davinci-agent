"""LIVE end-to-end color driver CLI (Todo 60): owned project, external QC.

Builds one owned ``__fvp_test__`` timeline from the declared-recipe bars
fixture whose own metadata drives the deterministic camera-preset
selection, applies the project color settings rung ONLY where probe
findings verified it, renders through the official Deliver API, and
validates color compliance from bytes (metadata + region luma) for both
the external conformance derivative and the Resolve render. Every run
deletes only the owned project and always leaves Resolve running.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final, cast

from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.presentation.color_live_flow import MANIFEST_DIR_NAME, ColorLiveFlow
from services.presentation.color_live_media import MARKER, ColorLiveError
from services.resolve_bridge.connection import BridgeConnectionError, connect
from services.resolve_bridge.lifecycle import cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report

EXIT_UNAVAILABLE: Final = 4
BRAND_A: Final = Path(MANIFEST_DIR_NAME) / "p3-brand-a.json"
BRAND_B: Final = Path(MANIFEST_DIR_NAME) / "p3-brand-b.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--ffprobe", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    missing = [
        name
        for name, value in (
            ("--evidence", arguments.evidence),
            ("--ffmpeg", arguments.ffmpeg),
            ("--ffprobe", arguments.ffprobe),
        )
        if value is None
    ]
    if missing:
        print(f"required: {missing}", file=sys.stderr)
        return 2
    evidence: Path = cast("Path", arguments.evidence)
    bundle = evidence / "color-live"
    bundle.mkdir(parents=True, exist_ok=True)
    try:
        report = load_host_report(arguments.report)
        connection = connect(report)
    except (BridgeConnectionError, OSError) as error:
        print(f"{MARKER} bridge unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    flow = ColorLiveFlow(
        connection=connection,
        bundle=bundle,
        ffmpeg=cast("Path", arguments.ffmpeg),
        ffprobe=cast("Path", arguments.ffprobe),
        manifest=Phase3FixtureManifest.model_validate_json(BRAND_A.read_bytes()),
        manifest_b=Phase3FixtureManifest.model_validate_json(BRAND_B.read_bytes()),
        render_deadline=arguments.timeout,
    )
    try:
        flow.run()
    except (ColorLiveError, OSError, ValueError) as error:
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
