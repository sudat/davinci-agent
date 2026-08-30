"""Server-probe seam of ``mcp-doctor`` (split from ``mcp_doctor.py``).

Spawns the pinned stdio server once per doctor run, probes ``initialize``
plus one ``resolve_control get_version`` tool call, and maps the raw probe
to typed doctor sections. Split out to keep ``mcp_doctor.py`` under the
module LOC ceiling after Task 11 added the committed-surface check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli.mcp_doctor_report import DoctorPaths, DoctorSection, fail_section, ok_section
from services.cli.mcp_probe import StdioProbe, probe_stdio_server

if TYPE_CHECKING:
    from services.toolchain.mcp_pin import McpPin

RESOLVE_PROBE_TOOL: Final = "resolve_control"
RESOLVE_PROBE_ARGUMENTS: Final = {"action": "get_version"}


def resolve_result_section(probe: StdioProbe) -> DoctorSection:
    """Map the resolve-version tool reply (or its absence) to a section."""
    if probe.resolve_response is None:
        return fail_section(
            "resolve_connection",
            "resolve-unreachable",
            f"no tool reply within {probe.timeout_seconds}s",
        )
    result = probe.resolve_response.get("result")
    if isinstance(result, dict) and not result.get("isError", False):
        text = ""
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = str(content[0].get("text", ""))
        return ok_section("resolve_connection", text or "resolve version call ok")
    return fail_section(
        "resolve_connection",
        "resolve-unreachable",
        f"{RESOLVE_PROBE_TOOL} failed: {json.dumps(result or probe.resolve_response)}"[:180],
    )


def probe_server(
    paths: DoctorPaths, pin: McpPin | None, probe_timeout: float
) -> tuple[DoctorSection, DoctorSection]:
    """Spawn the pinned stdio server; probe initialize, then Resolve via a tool call."""
    venv_ok = pin is not None and Path(pin.venv_python).is_file()
    entry_ok = pin is not None and (paths.clone_dir / pin.server_entry_point).is_file()
    if pin is None or not venv_ok or not entry_ok:
        missing = "pin unavailable" if pin is None else "venv interpreter or entry point missing"
        return fail_section(
            "server_alive", "server-probe-skipped", f"not spawnable: {missing}"
        ), fail_section(
            "resolve_connection", "resolve-probe-skipped", "server did not answer initialize"
        )
    probe = probe_stdio_server(
        [str(Path(pin.venv_python)), str(paths.clone_dir / pin.server_entry_point)],
        cwd=paths.clone_dir,
        timeout_seconds=probe_timeout,
        resolve_tool=RESOLVE_PROBE_TOOL,
        resolve_arguments=RESOLVE_PROBE_ARGUMENTS,
    )
    if probe.initialize_response is None:
        if probe.failure == "exit":
            server_section = fail_section(
                "server_alive", "server-exit", f"exited rc={probe.exit_code}"
            )
        else:
            server_section = fail_section(
                "server_alive", "server-timeout", f"no initialize response in {probe_timeout}s"
            )
        return server_section, fail_section(
            "resolve_connection", "resolve-probe-skipped", "server did not answer initialize"
        )
    server_section = ok_section(
        "server_alive",
        f"answered initialize within {probe_timeout}s: "
        f"{json.dumps(probe.initialize_response)[:180]}",
    )
    return server_section, resolve_result_section(probe)


__all__ = [
    "RESOLVE_PROBE_ARGUMENTS",
    "RESOLVE_PROBE_TOOL",
    "probe_server",
    "resolve_result_section",
]
