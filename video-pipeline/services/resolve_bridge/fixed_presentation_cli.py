"""Live CLI for the fixed-presentation spike: connect, run, and record evidence."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import ValidationError

from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.base_cut_plan import (
    BaseCutError,
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.connection import BridgeConnectionError, connect
from services.resolve_bridge.evidence_recorder import CliRunRecord, record_cli_run
from services.resolve_bridge.fixed_presentation import run_fixed_presentation
from services.resolve_bridge.fixed_presentation_evidence import stdout_lines, write_evidence
from services.resolve_bridge.fixed_presentation_models import (
    EXIT_UNAVAILABLE,
    MARKER,
    SUBTITLE_SRT,
)
from services.resolve_bridge.fixed_presentation_tools import MediaTools, RenderError
from services.resolve_bridge.readiness import load_host_report


def _default_bin(report_path: Path, name: str) -> Path:
    return report_path.resolve().parent / "bootstrap/ffmpeg-7.1.1/bin" / name


def _env_bin(key: str, report_path: Path, name: str) -> Path:
    default = str(_default_bin(report_path, name))
    return Path(os.environ.get(key, default))


def _argv(arguments: dict[str, Path | None]) -> list[str]:
    argv = [sys.executable, "-m", "services.resolve_bridge.fixed_presentation"]
    for key, value in arguments.items():
        if value is not None:
            argv += [f"--{key.replace('_', '-')}", str(value)]
    return argv


def run_cli(
    manifest_path: Path,
    fixture_dir: Path,
    report_path: Path,
    render_dir: Path,
    ffmpeg: Path | None,
    ffprobe: Path | None,
    evidence: Path | None,
) -> int:
    arguments = {
        "manifest": manifest_path,
        "fixture_dir": fixture_dir,
        "report": report_path,
        "render_dir": render_dir,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "evidence": evidence,
    }
    argv = _argv(arguments)
    ffmpeg_bin = ffmpeg or _env_bin("FVP_FFMPEG_BIN", report_path, "ffmpeg")
    ffprobe_bin = ffprobe or _env_bin("FVP_FFPROBE_BIN", report_path, "ffprobe")
    tools = MediaTools(ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
    try:
        manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
        media = fixture_media_map(fixture_dir)
        request = request_from_ir(ir_from_manifest(manifest), media)
        expected = expected_from_manifest(manifest, media)
        srt_path = fixture_dir / SUBTITLE_SRT
        if not srt_path.is_file():
            print(f"{MARKER} FAIL invalid inputs: subtitle missing: {srt_path}", file=sys.stderr)
            return 1
    except (OSError, ValidationError, BaseCutError) as error:
        print(f"{MARKER} FAIL invalid inputs: {error}", file=sys.stderr)
        return 1
    try:
        connection = connect(load_host_report(report_path))
        run = run_fixed_presentation(
            connection=connection,
            request=request,
            expected=expected,
            manifest=manifest,
            media=media,
            tools=tools,
            render_dir=render_dir,
            srt_path=srt_path,
            direct_allowed=False,
        )
        report, render_summary = run.report, run.render_summary
    except BridgeConnectionError as error:
        stdout = f"{MARKER} needs-live SKIPPED reason={error}\n"
        print(stdout, end="")
        _record(evidence, argv, EXIT_UNAVAILABLE, stdout, f"{MARKER} needs-live")
        return 0
    except (BaseCutError, RenderError, ValidationError, OSError) as error:
        print(f"{MARKER} FAIL build error: {error}", file=sys.stderr)
        return 1
    stdout = stdout_lines(report, render_summary)
    print(stdout, end="")
    if evidence is not None:
        write_evidence(evidence, report)
    _record(evidence, argv, 0 if report.passed else 1, stdout, MARKER)
    return 0 if report.passed else 1


def _record(evidence: Path | None, argv: list[str], code: int, stdout: str, observe: str) -> None:
    if evidence is None:
        return
    record_cli_run(
        evidence,
        CliRunRecord(
            argv=argv, exit_code=code, expected_exit=code, stdout=stdout, stderr="", observe=observe
        ),
    )
