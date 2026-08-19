"""LIVE end-to-end overlay driver CLI (Todo 58): owned project, dual path, pixels.

Builds one owned ``__fvp_test__`` timeline with the phase-0A fixture base
cut plus (a) an EXTERNAL transparent overlay media item on the top video
track (pinned-ffmpeg ProRes 4444 render from the registry asset) and (b)
the FUSION title path when — and only when — the published probe findings
verify template placement + control readback (the fusion apply runs in a
watchdog-guarded child; a stall is an honest recorded fallback, never a
fake success). The final render is then verified from PIXELS: region
coverage at anchor frames proves presence, position, and duration. API
return values alone never mark an item applied. Every run deletes only the
owned project and always leaves Resolve running.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from services.foundation_io import sha256_file
from services.presentation.overlay_live_flow import OverlayFlow
from services.presentation.overlay_live_fusion import _fusion_child
from services.presentation.overlay_live_media import MARKER, OverlayLiveError, _requests
from services.presentation.overlay_paths import fusion_support_from_matrix
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    connect,
)
from services.resolve_bridge.lifecycle import cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report

EXIT_UNAVAILABLE: Final = 4
DEFAULT_MATRIX: Final = Path("capabilities/resolve-21.0.4/title-probe-findings.json")
LOGO_ASSET: Final = Path(
    "tests/fixtures/manifests/phase-3/assets/p3-brand-a/logo.mov"
)
PROBE_REPORT_NAME: Final = "title-probe/title-probe-report.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--ffprobe", type=Path, default=None)
    parser.add_argument("--base-source", type=Path, default=None)
    parser.add_argument("--overlay-asset", type=Path, default=None)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--child-fusion", action="store_true")
    parser.add_argument("--project-name")
    parser.add_argument("--timeline-name")
    parser.add_argument("--result-json", type=Path)
    return parser


def main() -> int:  # noqa: PLR0911 (live driver CLI dispatch)
    arguments = _parser().parse_args()
    if arguments.child_fusion:
        if (
            arguments.result_json is None
            or arguments.project_name is None
            or arguments.timeline_name is None
        ):
            print("--result-json/--project-name/--timeline-name required", file=sys.stderr)
            return 2
        try:
            return _fusion_child(
                arguments.report,
                arguments.result_json,
                arguments.project_name,
                arguments.timeline_name,
            )
        except (OverlayLiveError, BridgeConnectionError, OSError) as error:
            print(f"{MARKER} fusion child failed: {error}", file=sys.stderr)
            return 1
    missing = [
        name
        for name, value in (
            ("--evidence", arguments.evidence),
            ("--ffmpeg", arguments.ffmpeg),
            ("--ffprobe", arguments.ffprobe),
            ("--base-source", arguments.base_source),
            ("--overlay-asset", arguments.overlay_asset),
        )
        if value is None
    ]
    if missing:
        print(f"required outside child mode: {missing}", file=sys.stderr)
        return 2
    evidence: Path = arguments.evidence
    bundle = evidence / "overlay-live"
    bundle.mkdir(parents=True, exist_ok=True)
    try:
        report = load_host_report(arguments.report)
        connection = connect(report)
    except (BridgeConnectionError, OSError) as error:
        print(f"{MARKER} bridge unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    ffmpeg_bin = arguments.ffmpeg
    ffprobe_bin = arguments.ffprobe
    base_source = arguments.base_source
    overlay_asset = arguments.overlay_asset
    if (
        ffmpeg_bin is None
        or ffprobe_bin is None
        or base_source is None
        or overlay_asset is None
    ):
        print("internal error: required paths missing after validation", file=sys.stderr)
        return 2
    probe_report = evidence / PROBE_REPORT_NAME
    probe_sha = sha256_file(probe_report) if probe_report.is_file() else "0" * 64
    overlay_sha = sha256_file(overlay_asset)
    flow = OverlayFlow(
        connection=connection,
        bundle=bundle,
        host_report=arguments.report,
        ffmpeg=arguments.ffmpeg,
        ffprobe=arguments.ffprobe,
        base_source=arguments.base_source,
        overlay_asset=arguments.overlay_asset,
        overlay_asset_sha=overlay_sha,
        support=fusion_support_from_matrix(arguments.matrix),
        item_requests=_requests(probe_sha, overlay_sha, sha256_file(LOGO_ASSET)),
        render_deadline=arguments.timeout,
    )
    try:
        flow.run()
    except (OverlayLiveError, OSError, ValueError) as error:
        print(f"{MARKER} FAIL {error}", file=sys.stderr)
    finally:
        try:
            cleanup_owned_projects(connection.project_manager())
        except Exception as error:  # noqa: BLE001 (cleanup must not mask failures)
            print(f"{MARKER} cleanup warning: {error}", file=sys.stderr)
    print(f"{MARKER} {'PASS' if flow.passed else 'FAIL'} fusion label={flow.fusion_label}")
    return 0 if flow.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
