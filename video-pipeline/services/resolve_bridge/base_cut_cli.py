"""Live CLI for the base-cut spike: connect, build, compare, and record evidence."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes
from services.resolve_bridge.base_cut import EXIT_UNAVAILABLE, build_base_cut
from services.resolve_bridge.base_cut_compare import (
    BaseCutReport,
    compare,
    delta_lines,
    summary_line,
)
from services.resolve_bridge.base_cut_plan import (
    BaseCutError,
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.connection import BridgeConnectionError, connect
from services.resolve_bridge.evidence_recorder import CliRunRecord, record_cli_run
from services.resolve_bridge.readiness import load_host_report

MARKER = "base-cut:"
REPORT_NAME = "base-cut-report.json"


def _argv(manifest: Path, fixture_dir: Path, report: Path, evidence: Path | None) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "services.resolve_bridge.base_cut",
        "--manifest",
        str(manifest),
        "--fixture-dir",
        str(fixture_dir),
        "--report",
        str(report),
    ]
    return argv + (["--evidence", str(evidence)] if evidence is not None else [])


def _record(evidence: Path | None, argv: list[str], code: int, stdout: str, observe: str) -> None:
    if evidence is None:
        return
    record_cli_run(
        evidence,
        CliRunRecord(
            argv=argv, exit_code=code, expected_exit=code, stdout=stdout, stderr="", observe=observe
        ),
    )


def run_cli(
    manifest_path: Path, fixture_dir: Path, report_path: Path, evidence: Path | None
) -> int:
    argv = _argv(manifest_path, fixture_dir, report_path, evidence)
    try:
        manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
        media = fixture_media_map(fixture_dir)
        request = request_from_ir(ir_from_manifest(manifest), media)
        expected = expected_from_manifest(manifest, media)
    except (OSError, ValidationError, BaseCutError) as error:
        print(f"{MARKER} FAIL invalid inputs: {error}", file=sys.stderr)
        return 1
    try:
        connection = connect(load_host_report(report_path))
        snapshot = build_base_cut(connection, request)
    except BridgeConnectionError as error:
        stdout = f"{MARKER} needs-live SKIPPED reason={error}\n"
        print(stdout, end="")
        _record(evidence, argv, EXIT_UNAVAILABLE, stdout, f"{MARKER} needs-live")
        return 0
    except (BaseCutError, OSError) as error:
        print(f"{MARKER} FAIL build error: {error}", file=sys.stderr)
        return 1
    outcome = compare(expected, snapshot)
    lines = [
        summary_line(outcome),
        *delta_lines(outcome),
        (f"{MARKER} record_frames={snapshot.record_frame_count} "
        f"tracks={snapshot.video_track_count}/{snapshot.audio_track_count}/"
        f"{snapshot.subtitle_track_count}"),
    ]
    if outcome.passed:
        lines.append(f"{MARKER} PASS fixture=p0a-cfr30-fixed deltas=0")
    else:
        lines.extend(f"{MARKER} FAIL code={m.code} {m.detail}" for m in outcome.mismatches)
    stdout = "\n".join(lines) + "\n"
    print(stdout, end="")
    if evidence is not None:
        atomic_write(
            evidence / REPORT_NAME,
            canonical_model_bytes(
                BaseCutReport(
                    schema_version="base-cut-report-v1",
                    requested=expected,
                    built=snapshot,
                    outcome=outcome,
                )
            ),
        )
    _record(evidence, argv, 0 if outcome.passed else 1, stdout, MARKER)
    return 0 if outcome.passed else 1
