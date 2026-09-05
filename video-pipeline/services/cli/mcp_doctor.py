"""``mcp-doctor`` — verify the pinned davinci-resolve-mcp deployment.

Every check is an explicit typed section (never silently downgraded to ok):
pin, clone, HEAD SHA, venv, entry point, ffmpeg, stdio server liveness,
Resolve reachability, advanced binary, Node runtime, update-check policy.
JSON to stdout; an unreachable Resolve prints the remedy hint to stderr and
exits nonzero.
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
from services.toolchain.mcp_pin import McpPin, McpPinError, load_mcp_pin
from services.toolchain.mcp_surface_gate import committed_surface_status

REPORT_SCHEMA: Final = "mcp-doctor-v1"
GIT_TIMEOUT_SECONDS: Final = 10.0
VERSION_TIMEOUT_SECONDS: Final = 10.0
RESOLVE_REMEDY_HINT: Final = (
    "DaVinci Resolve: Preferences > General > External scripting using = Local, and launch Resolve"
)

_FFmpegLookup = Callable[[], str | None]


def _check_pin(paths: DoctorPaths) -> tuple[DoctorSection, McpPin | None]:
    try:
        pin = load_mcp_pin(paths.pin_path)
    except McpPinError as exc:
        return fail_section("pin_json", "pin-invalid", str(exc)), None
    return ok_section("pin_json", f"{paths.pin_path.name} matches mcp-pin-v1 at {pin.commit}"), pin


def _check_clone(paths: DoctorPaths) -> DoctorSection:
    if paths.clone_dir.is_dir():
        return ok_section("clone_exists", str(paths.clone_dir))
    return fail_section(
        "clone_exists", "clone-failed", f"clone directory absent: {paths.clone_dir}"
    )


def _check_head(paths: DoctorPaths, pin: McpPin | None) -> DoctorSection:
    if pin is None:
        return fail_section("head_sha", "pin-invalid", "pin unavailable; cannot compare HEAD")
    try:
        completed = subprocess.run(
            ["git", "-C", str(paths.clone_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return fail_section("head_sha", "head-unreadable", "git rev-parse timed out")
    if completed.returncode != 0:
        return fail_section(
            "head_sha", "head-unreadable", f"git rev-parse failed: {completed.stderr.strip()}"
        )
    head = completed.stdout.strip()
    if head == pin.commit:
        return ok_section("head_sha", head)
    return fail_section("head_sha", "sha-mismatch", f"HEAD {head} != pinned {pin.commit}")


def _check_venv(paths: DoctorPaths, pin: McpPin | None) -> DoctorSection:
    if pin is None:
        return fail_section(
            "venv_python", "pin-invalid", "pin unavailable; cannot locate venv python"
        )
    venv = Path(pin.venv_python)
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


def _check_entry(paths: DoctorPaths, pin: McpPin | None) -> DoctorSection:
    if pin is None:
        return fail_section(
            "server_entry_point", "pin-invalid", "pin unavailable; cannot locate entry point"
        )
    entry = paths.clone_dir / pin.server_entry_point
    if entry.is_file():
        return ok_section("server_entry_point", str(entry))
    return fail_section("server_entry_point", "entry-point-missing", f"entry point absent: {entry}")


def _check_ffmpeg(lookup: _FFmpegLookup) -> DoctorSection:
    if found := lookup():
        return ok_section("ffmpeg", found)
    return fail_section("ffmpeg", "ffmpeg-missing", "ffmpeg not found on PATH")


def _check_advanced(paths: DoctorPaths, pin: McpPin | None) -> DoctorSection:
    if pin is None:
        return fail_section("advanced_binary", "pin-invalid", "pin unavailable")
    if not pin.advanced_server.enabled:
        return ok_section("advanced_binary", "advanced server disabled by pin")
    binary = paths.clone_dir / "bin" / "davinci-resolve-advanced-mcp.mjs"
    package = paths.clone_dir / "resolve-advanced" / "package.json"
    if binary.is_file() or package.is_file():
        return ok_section("advanced_binary", f"{binary} (package {pin.advanced_server.package})")
    return fail_section(
        "advanced_binary", "advanced-binary-missing", f"neither {binary} nor {package} exists"
    )


def _check_update_state(paths: DoctorPaths, pin: McpPin | None) -> DoctorSection:
    if pin is None:
        return fail_section("update_check", "pin-invalid", "pin unavailable")
    state_path = paths.clone_dir / "logs" / "update-check.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        missing = f"no persisted policy at {state_path}"
        return fail_section("update_check", "update-state-missing", missing)
    if state.get("update_mode") == "never":
        return ok_section(
            "update_check", f"update_mode=never at {state_path} ({pin.update_check.mechanism})"
        )
    return fail_section(
        "update_check", "update-check-enabled", f"update_mode={state.get('update_mode')!r}"
    )


def _check_tool_surface(paths: DoctorPaths, pin: McpPin | None) -> DoctorSection:
    """Committed inventory/dispositions/manifest + checkout agreement (Task 11)."""
    if pin is None:
        return fail_section(
            "tool_surface", "pin-invalid", "pin unavailable; cannot validate surface"
        )
    status = committed_surface_status(
        pin_path=paths.pin_path,
        clone_dir=paths.clone_dir,
        coverage_dir=paths.coverage_dir,
    )
    if status.ok:
        return ok_section("tool_surface", status.detail)
    return fail_section("tool_surface", status.code, status.detail)


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
    pin_section, pin = _check_pin(paths)
    sections = (
        pin_section,
        _check_clone(paths),
        _check_head(paths, pin),
        _check_venv(paths, pin),
        _check_entry(paths, pin),
        _check_ffmpeg(lookup),
        *probe_server(paths, pin, probe_timeout),
        _check_tool_surface(paths, pin),
        _check_advanced(paths, pin),
        check_node(nodes),
        _check_update_state(paths, pin),
    )
    ok = all(section.status == "ok" for section in sections)
    return DoctorReport(
        schema_version=REPORT_SCHEMA, ok=ok, exit_code=0 if ok else 1, sections=sections
    )


def main(argv: list[str] | None = None) -> int:
    video_pipeline_root = Path(__file__).resolve().parents[2]
    default_clone = video_pipeline_root.parent / "private" / "vendor" / "davinci-resolve-mcp"
    default_pin = video_pipeline_root / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
    parser = argparse.ArgumentParser(prog="mcp-doctor", description=__doc__)
    parser.add_argument("--clone", type=Path, default=default_clone)
    parser.add_argument("--pin", type=Path, default=default_pin)
    parser.add_argument(
        "--repo-venv", type=Path, default=video_pipeline_root / ".venv" / "bin" / "python"
    )
    parser.add_argument("--probe-timeout", type=float, default=10.0)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    args = parser.parse_args(argv)

    lookup = (lambda: str(args.ffmpeg)) if args.ffmpeg is not None else None
    report = run_doctor(
        DoctorPaths(clone_dir=args.clone, pin_path=args.pin, repo_venv_python=args.repo_venv),
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
