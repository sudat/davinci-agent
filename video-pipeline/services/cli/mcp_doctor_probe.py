"""Server-probe seam of ``mcp-doctor`` (split from ``mcp_doctor.py``).

Spawns the vendored stdio server once per doctor run, probes ``initialize``
plus ``tools/list`` plus one ``resolve_control get_version`` tool call, and
maps the raw probe to typed doctor sections. Verifies the handshake server
name (the reported version is surfaced, never enforced).
"""

from __future__ import annotations

import json
from typing import Final

from services.cli.mcp_doctor_report import DoctorPaths, DoctorSection, fail_section, ok_section
from services.cli.mcp_probe import StdioProbe, probe_stdio_server
from services.mcp_client.discovery import (
    EXPECTED_SERVER_NAME,
    McpServerNameError,
    verify_server_name,
)

RESOLVE_PROBE_TOOL: Final = "resolve_control"
RESOLVE_PROBE_ARGUMENTS: Final = {"action": "get_version"}


def identity_section(probe: StdioProbe) -> DoctorSection:
    """Map the initialize ``serverInfo`` to a section (name enforced)."""
    try:
        handshake = verify_server_name(
            probe.initialize_response.get("result")
            if isinstance(probe.initialize_response, dict)
            else None
        )
    except McpServerNameError as exc:
        return fail_section("server_identity", "server-identity-mismatch", str(exc))
    info = handshake.server_info
    return ok_section(
        "server_identity",
        f"{info.name} version {info.version} (reported, not enforced)",
    )


def tool_list_section(probe: StdioProbe) -> DoctorSection:
    """Map the ``tools/list`` reply (or its absence) to a section."""
    result = probe.tools_response.get("result") if isinstance(probe.tools_response, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if isinstance(tools, list):
        return ok_section("tool_list", f"{len(tools)} tools")
    return fail_section(
        "tool_list", "tools-list-failed", "no tools/list reply within the deadline"
    )


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
    paths: DoctorPaths, probe_timeout: float
) -> tuple[DoctorSection, DoctorSection, DoctorSection, DoctorSection]:
    """Spawn the vendored stdio server; probe initialize, tools, Resolve."""
    venv_python = paths.venv_python
    entry_point = paths.clone_dir / "src" / "server.py"
    if not venv_python.is_file() or not entry_point.is_file():
        missing = "venv interpreter or entry point missing"
        return (
            fail_section("server_alive", "server-probe-skipped", f"not spawnable: {missing}"),
            fail_section("server_identity", "server-probe-skipped", f"not spawnable: {missing}"),
            fail_section("tool_list", "server-probe-skipped", f"not spawnable: {missing}"),
            fail_section(
                "resolve_connection", "resolve-probe-skipped", "server did not answer initialize"
            ),
        )
    probe = probe_stdio_server(
        [str(venv_python), str(entry_point)],
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
        return (
            server_section,
            fail_section(
                "server_identity", "resolve-probe-skipped", "server did not answer initialize"
            ),
            fail_section(
                "tool_list", "resolve-probe-skipped", "server did not answer initialize"
            ),
            fail_section(
                "resolve_connection", "resolve-probe-skipped", "server did not answer initialize"
            ),
        )
    server_section = ok_section(
        "server_alive",
        f"answered initialize within {probe_timeout}s",
    )
    return (
        server_section,
        identity_section(probe),
        tool_list_section(probe),
        resolve_result_section(probe),
    )


__all__ = [
    "EXPECTED_SERVER_NAME",
    "RESOLVE_PROBE_ARGUMENTS",
    "RESOLVE_PROBE_TOOL",
    "identity_section",
    "probe_server",
    "resolve_result_section",
    "tool_list_section",
]
