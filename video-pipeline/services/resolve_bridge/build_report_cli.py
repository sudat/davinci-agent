"""CLI entrypoints for the Build-Report 0A spike writer.

``services.resolve_bridge.build_report`` (run mode) performs one live spike
run and writes the report plus hash-chained evidence; ``--verify`` recomputes
an existing report from raw fields and the render bytes; fault mode via
``QA_FAULT_FIXTURE`` exercises the offline adversarial scenarios.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.contracts.build_report import BuildReport0A
from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes
from services.resolve_bridge.base_cut_plan import (
    BaseCutError,
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.build_report import run_spike
from services.resolve_bridge.build_report_models import MARKER, VerifyOutcome
from services.resolve_bridge.build_report_verify import verify_report
from services.resolve_bridge.connection import BridgeConnectionError, connect
from services.resolve_bridge.evidence_recorder import CliRunRecord, record_cli_run
from services.resolve_bridge.fixed_presentation_models import EXIT_UNAVAILABLE, SUBTITLE_SRT
from services.resolve_bridge.fixed_presentation_tools import MediaTools, RenderError
from services.resolve_bridge.readiness import load_host_report

MODULE: Final = "services.resolve_bridge.build_report"
BOOTSTRAP_BIN: Final = "bootstrap/ffmpeg-7.1.1/bin"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--host-report", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser


def _media_bin(env_key: str, name: str, host_report: Path | None) -> Path:
    override = os.environ.get(env_key)
    if override:
        return Path(override)
    if host_report is not None:
        return host_report.resolve().parent / BOOTSTRAP_BIN / name
    raise ValueError(f"--{name} or {env_key} is required without --host-report")


def _module_argv() -> list[str]:
    return [sys.executable, "-m", MODULE, *sys.argv[1:]]


def cli_main() -> int:
    arguments = _parser().parse_args()
    fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
    if fault_fixture is not None:
        from services.resolve_bridge.build_report_faults import run_fault_cli  # noqa: PLC0415

        return run_fault_cli(Path(fault_fixture), arguments.manifest, arguments.fixture_dir)
    try:
        ffmpeg = arguments.ffmpeg or _media_bin("FVP_FFMPEG_BIN", "ffmpeg", arguments.host_report)
        ffprobe = arguments.ffprobe or _media_bin(
            "FVP_FFPROBE_BIN", "ffprobe", arguments.host_report
        )
    except ValueError as error:
        print(f"{MARKER} FAIL {error}", file=sys.stderr)
        return 2
    if arguments.verify is not None:
        return _verify_cli(arguments, ffmpeg, ffprobe)
    return _live_cli(arguments, ffmpeg, ffprobe)


def _verify_cli(arguments: argparse.Namespace, ffmpeg: Path, ffprobe: Path) -> int:
    if arguments.host_report is None:
        print(f"{MARKER} FAIL --host-report is required with --verify", file=sys.stderr)
        return 2
    try:
        manifest = Phase0AFixtureManifest.model_validate_json(arguments.manifest.read_bytes())
        report = BuildReport0A.model_validate_json(arguments.verify.read_bytes())
    except (OSError, ValidationError) as error:
        print(f"{MARKER} FAIL code=report-schema-invalid {error}", file=sys.stderr)
        return 1
    tools = MediaTools(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe)
    outcome = verify_report(
        report,
        manifest,
        arguments.fixture_dir,
        tools,
        manifest_path=arguments.manifest,
        host_report_path=arguments.host_report,
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )
    for line in outcome.lines():
        print(line)
    verdict = "PASS" if outcome.passed else "FAIL"
    print(f"{MARKER} {verdict} verify={arguments.verify}")
    return 0 if outcome.passed else 1


def _live_cli(arguments: argparse.Namespace, ffmpeg: Path, ffprobe: Path) -> int:
    if arguments.host_report is None or arguments.out is None or arguments.render_dir is None:
        print(
            f"{MARKER} FAIL --host-report, --out, and --render-dir are required outside fault mode",
            file=sys.stderr,
        )
        return 2
    argv = _module_argv()
    try:
        manifest = Phase0AFixtureManifest.model_validate_json(arguments.manifest.read_bytes())
        media = fixture_media_map(arguments.fixture_dir)
        request = request_from_ir(ir_from_manifest(manifest), media)
        expected = expected_from_manifest(manifest, media)
    except (OSError, ValidationError, BaseCutError) as error:
        print(f"{MARKER} FAIL invalid inputs: {error}", file=sys.stderr)
        return 1
    srt_path = arguments.fixture_dir / SUBTITLE_SRT
    if not srt_path.is_file():
        print(f"{MARKER} FAIL invalid inputs: subtitle missing: {srt_path}", file=sys.stderr)
        return 1
    tools = MediaTools(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe)
    try:
        connection = connect(load_host_report(arguments.host_report))
        report = run_spike(
            connection,
            request,
            expected,
            manifest,
            media,
            tools,
            arguments.render_dir,
            srt_path,
            host_report_path=arguments.host_report,
            manifest_path=arguments.manifest,
            ffmpeg_bin=ffmpeg,
            ffprobe_bin=ffprobe,
        )
    except BridgeConnectionError as error:
        stdout = f"{MARKER} needs-live SKIPPED reason={error}\n"
        print(stdout, end="")
        _record_skipped(arguments.evidence, argv, stdout)
        return 0
    except (BaseCutError, RenderError, ValidationError, OSError) as error:
        print(f"{MARKER} FAIL build error: {error}", file=sys.stderr)
        return 1
    outcome = verify_report(
        report,
        manifest,
        arguments.fixture_dir,
        tools,
        manifest_path=arguments.manifest,
        host_report_path=arguments.host_report,
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )
    stdout = "\n".join(_report_lines(report, outcome)) + "\n"
    verdict = "PASS" if outcome.passed else "FAIL"
    stdout += f"{MARKER} {verdict} fixture={manifest.fixture_id}\n"
    print(stdout, end="")
    atomic_write(arguments.out, canonical_model_bytes(report))
    if arguments.evidence is not None:
        code = 0 if outcome.passed else 1
        record_cli_run(
            arguments.evidence,
            CliRunRecord(
                argv=argv,
                exit_code=code,
                expected_exit=code,
                stdout=stdout,
                stderr="",
                observe=f"{MARKER} {verdict}",
            ),
        )
    return 0 if outcome.passed else 1


def _report_lines(report: BuildReport0A, outcome: VerifyOutcome) -> list[str]:
    return [
        f"{MARKER} items={len(report.items)} fingerprint={report.timeline_fingerprint[:12]}",
        (
            f"{MARKER} render job={report.render_job.job_id} status=complete "
            f"completion=CompletionPercentage polls={report.render_job.poll_count}"
        ),
        (
            f"{MARKER} render output={report.render_output.output_path} "
            f"sha256={report.output_hash} decode-exit={report.render_output.decode.exit_code}"
        ),
        (
            f"{MARKER} binding resolve={report.bindings.resolve_version} "
            f"build={report.bindings.resolve_build} "
            f"adapter={report.bindings.adapter_version[:12]} "
            f"manifest={report.bindings.manifest_sha256[:12]} "
            f"host={report.bindings.host_report_sha256[:12]}"
        ),
        *(f"{MARKER} warning code={row.code}" for row in report.warnings),
        *outcome.lines(),
    ]


def _record_skipped(evidence: Path | None, argv: list[str], stdout: str) -> None:
    if evidence is None:
        return
    record_cli_run(
        evidence,
        CliRunRecord(
            argv=argv,
            exit_code=EXIT_UNAVAILABLE,
            expected_exit=EXIT_UNAVAILABLE,
            stdout=stdout,
            stderr="",
            observe=f"{MARKER} needs-live",
        ),
    )
