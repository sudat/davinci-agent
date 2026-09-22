"""``mcp-doctor`` — verify the vendored davinci-resolve-mcp deployment.

The server is tracked as a plain vendored checkout
(``private/vendor/davinci-resolve-mcp``) with no version-freeze contract, so
the doctor is a deployment health check, not a pin audit: checkout present,
venv independent, entry point present, ffmpeg, stdio server liveness,
handshake server name (the reported version is surfaced, never enforced),
tools/list, Resolve reachability, advanced binary, Node runtime, and the
update-check policy (``update_mode=never``). JSON to stdout; an unreachable
Resolve prints the remedy hint to stderr and exits nonzero.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

from services.cli.mcp_doctor_node import (
    NodeLookup,
    check_node,
    default_node_lookup,
)
from services.cli.mcp_doctor_probe import probe_server
from services.cli.mcp_doctor_report import (
    DoctorPaths,
    DoctorReport,
    DoctorSection,
    fail_section,
    ok_section,
)

REPORT_SCHEMA: Final = "mcp-doctor-v1"
VERSION_TIMEOUT_SECONDS: Final = 10.0
RESOLVE_REMEDY_HINT: Final = (
    "DaVinci Resolve: Preferences > General > External scripting using = Local, and launch Resolve"
)

_FFmpegLookup = Callable[[], str | None]


def _check_clone(paths: DoctorPaths) -> DoctorSection:
    if paths.clone_dir.is_dir():
        return ok_section("clone_exists", str(paths.clone_dir))
    return fail_section(
        "clone_exists", "clone-failed", f"clone directory absent: {paths.clone_dir}"
    )


def _check_venv(paths: DoctorPaths) -> DoctorSection:
    venv = paths.venv_python
    # Compare venv roots, never symlink-resolved targets: both venv pythons
    # symlink to one base interpreter; resolving reports them as one overlap.
    venv_absolute = venv.absolute()
    repo_absolute = paths.repo_venv_python.absolute()
    same_entry = venv_absolute == repo_absolute
    same_root = venv_absolute.parent.parent == repo_absolute.parent.parent
    if same_entry or same_root:
        return fail_section(
            "venv_python",
            "venv-overlaps-repo-venv",
            f"{venv} is the repo interpreter; the MCP venv must be independent",
        )
    if not venv.is_file():
        return fail_section("venv_python", "venv-missing", f"venv interpreter absent: {venv}")
    try:
        completed = subprocess.run(
            [str(venv), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return fail_section("venv_python", "venv-version-mismatch", "interpreter probe timed out")
    reported = completed.stdout.strip() or completed.stderr.strip()
    if not reported.startswith("Python 3.12."):
        return fail_section(
            "venv_python", "venv-version-mismatch", f"expected 3.12.x, got {reported!r}"
        )
    return ok_section("venv_python", f"{reported} at {venv} (independent of repo .venv)")


def _check_entry(paths: DoctorPaths) -> DoctorSection:
    entry = paths.clone_dir / "src" / "server.py"
    if entry.is_file():
        return ok_section("server_entry_point", str(entry))
    return fail_section("server_entry_point", "entry-point-missing", f"entry point absent: {entry}")


def _check_ffmpeg(lookup: _FFmpegLookup) -> DoctorSection:
    if found := lookup():
        return ok_section("ffmpeg", found)
    return fail_section("ffmpeg", "ffmpeg-missing", "ffmpeg not found on PATH")


def _check_advanced(paths: DoctorPaths) -> DoctorSection:
    binary = paths.clone_dir / "bin" / "davinci-resolve-advanced-mcp.mjs"
    package = paths.clone_dir / "resolve-advanced" / "package.json"
    if binary.is_file() or package.is_file():
        return ok_section("advanced_binary", str(binary))
    return fail_section(
        "advanced_binary", "advanced-binary-missing", f"neither {binary} nor {package} exists"
    )


def _check_update_state(paths: DoctorPaths) -> DoctorSection:
    state_path = paths.clone_dir / "logs" / "update-check.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        missing = f"no persisted policy at {state_path}"
        return fail_section("update_check", "update-state-missing", missing)
    if state.get("update_mode") == "never":
        return ok_section("update_check", f"update_mode=never at {state_path}")
    return fail_section(
        "update_check", "update-check-enabled", f"update_mode={state.get('update_mode')!r}"
    )


def run_doctor(
    paths: DoctorPaths,
    *,
    probe_timeout: float = 10.0,
    ffmpeg_lookup: _FFmpegLookup | None = None,
    node_lookup: NodeLookup | None = None,
) -> DoctorReport:
    """Run every check and return the typed report (never a silent downgrade)."""
    lookup: _FFmpegLookup = ffmpeg_lookup or (lambda: shutil.which("ffmpeg"))
    nodes: NodeLookup = node_lookup or default_node_lookup
    sections = (
        _check_clone(paths),
        _check_venv(paths),
        _check_entry(paths),
        _check_ffmpeg(lookup),
        *probe_server(paths, probe_timeout),
        _check_advanced(paths),
        check_node(nodes),
        _check_update_state(paths),
    )
    ok = all(section.status == "ok" for section in sections)
    return DoctorReport(
        schema_version=REPORT_SCHEMA, ok=ok, exit_code=0 if ok else 1, sections=sections
    )


def main(argv: list[str] | None = None) -> int:
    video_pipeline_root = Path(__file__).resolve().parents[2]
    default_clone = video_pipeline_root.parent / "private" / "vendor" / "davinci-resolve-mcp"
    parser = argparse.ArgumentParser(prog="mcp-doctor", description=__doc__)
    parser.add_argument("--clone", type=Path, default=default_clone)
    parser.add_argument(
        "--repo-venv", type=Path, default=video_pipeline_root / ".venv" / "bin" / "python"
    )
    parser.add_argument(
        "--venv", type=Path, default=None, help="MCP venv python (default <clone>/venv/bin/python)"
    )
    parser.add_argument("--probe-timeout", type=float, default=10.0)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    args = parser.parse_args(argv)

    venv_python = args.venv if args.venv is not None else args.clone / "venv" / "bin" / "python"
    lookup = (lambda: str(args.ffmpeg)) if args.ffmpeg is not None else None
    report = run_doctor(
        DoctorPaths(
            clone_dir=args.clone, repo_venv_python=args.repo_venv, venv_python=venv_python
        ),
        probe_timeout=args.probe_timeout,
        ffmpeg_lookup=lookup,
    )
    print(report.to_json())
    for section in report.sections:
        if section.check == "resolve_connection" and section.code == "resolve-unreachable":
            print(RESOLVE_REMEDY_HINT, file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["DoctorPaths", "DoctorReport", "DoctorSection", "main", "run_doctor"]
