"""Shared Task 11 fixtures: mini vendor trees + committed coverage artifacts.

The surface-gate tests (client, doctor, toolchain) all need the same three
sides of one committed baseline: a statically parseable mini vendor checkout
(``src/server.py`` + ``src/granular/`` + ``docs/kernels/README.md``), a
committed ``capabilities/mcp-coverage``-style artifact triple, and a pin file.
Everything is synthetic (tmp_path, no network, no server spawn).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from services.release.manifest import build_manifest, manifest_bytes
from services.toolchain.mcp_coverage import inventory_bytes, schema_sha256
from services.toolchain.mcp_coverage_models import (
    INVENTORY_SCHEMA,
    CompoundToolRow,
    CrossCheckFacts,
    GranularToolRow,
    InventoryCounts,
    McpInventoryV1,
    OperationRow,
    PinFacts,
)

MINI_PROVIDER_VERSION = "2.98.3"
OTHER_SHA = "1111111111111111111111111111111111111111"

_SERVER_TEMPLATE = """\
VERSION = "{version}"

_TOKEN_GATED_DESTRUCTIVE_ACTIONS = frozenset({{
    ("resolve_control", "delete_everything"),
}})


def _unknown(action, valid):
    return {{"error": "UNKNOWN_ACTION"}}


def _annotations_for_tool_name(tool_name):
    external_tools = ()
    destructive_tools = ()
    if tool_name in external_tools:
        return "external_destructive"
    if tool_name in destructive_tools:
        return "destructive"
    return "write"


{tools}
"""

_TOOL_DEF = """\
@mcp.tool()
def {name}(action: str, params=None):
    return _unknown(action, {actions})
"""

_KERNEL_README = """\
# Kernel Action Coverage

Current kernel coverage: **1 actions** across **1 compound MCP tools**.

| Kernel | MCP Tool | Actions |
|--------|----------|---------|
| Edit | `resolve_control` | `get_version` |
"""


def static_server_source(
    tools: Mapping[str, tuple[str, ...]], *, version: str = MINI_PROVIDER_VERSION
) -> str:
    """A parseable ``src/server.py`` body for the given compound tools."""
    defs = "\n\n".join(
        _TOOL_DEF.format(name=name, actions=json.dumps(list(actions)))
        for name, actions in sorted(tools.items())
    )
    return _SERVER_TEMPLATE.format(version=version, tools=defs)


def write_mini_clone(
    tmp_path: Path,
    tools: Mapping[str, tuple[str, ...]],
    *,
    version: str = MINI_PROVIDER_VERSION,
) -> Path:
    """Write the mini vendor checkout the gate statically parses."""
    clone = tmp_path / "clone"
    (clone / "src" / "granular").mkdir(parents=True, exist_ok=True)
    (clone / "docs" / "kernels").mkdir(parents=True, exist_ok=True)
    (clone / "src" / "server.py").write_text(static_server_source(tools, version=version))
    (clone / "src" / "granular" / "common.py").write_text("mcp = None\n")
    (clone / "src" / "granular" / "misc.py").write_text(
        "@mcp.tool()\ndef probe() -> str:\n    return 'probe'\n"
    )
    (clone / "docs" / "kernels" / "README.md").write_text(_KERNEL_README)
    return clone


def git_commit_clone(clone: Path) -> str:
    """Init a git repo over the fixture tree; returns the HEAD sha."""
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=clone, check=True)
    return git_head(clone)


def git_head(clone: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def mini_inventory(
    *,
    pin_commit: str,
    provider_version: str = MINI_PROVIDER_VERSION,
    schemas: Mapping[str, str],
    actions: Mapping[str, tuple[str, ...]],
) -> McpInventoryV1:
    operations = tuple(
        OperationRow(
            operation_id=f"{tool}.{action}",
            tool=tool,
            action=action,
            source="compound",
            compound_aliases=(f"{tool}.{action}",),
            granular_aliases=(),
            safety="write",
            requires_confirm_token=False,
            kernel=False,
        )
        for tool, tool_actions in sorted(actions.items())
        for action in tool_actions
    )
    return McpInventoryV1(
        schema_version=INVENTORY_SCHEMA,
        pin=PinFacts(
            commit=pin_commit,
            provider_version=provider_version,
            server_mode="compound",
            advanced_enabled=False,
            handshake_name="DaVinciResolveMCP",
            handshake_version="1.29.0",
        ),
        counts=InventoryCounts(
            compound_tools=len(actions), granular_tools=1, kernel_actions=1
        ),
        compound_tools=tuple(
            CompoundToolRow(
                name=tool,
                input_schema_sha256=schemas[tool],
                safety="write",
                actions=tuple(tool_actions),
            )
            for tool, tool_actions in sorted(actions.items())
        ),
        granular_tools=(GranularToolRow(module="misc", name="probe"),),
        operations=operations,
        kernel_catalog_stated_actions=1,
        kernel_catalog_stated_tools=1,
        kernel_catalog_rows=1,
        cross_check=CrossCheckFacts(
            static_tool_names_match_live=True,
            compound_action_count=len(operations),
            operation_count=len(operations),
            granular_alias_count=0,
            granular_only=("probe",),
            kernel_action_orphans=(),
            kernel_tool_orphans=(),
            intra_tool_duplicate_actions=(),
        ),
    )


def write_coverage_pair(
    tmp_path: Path,
    *,
    pin_commit: str,
    schemas: Mapping[str, str],
    actions: Mapping[str, tuple[str, ...]],
    provider_version: str = MINI_PROVIDER_VERSION,
) -> tuple[Path, str]:
    """Write the committed artifact triple (canonical bytes + manifest seal)."""
    coverage = tmp_path / "coverage"
    coverage.mkdir(parents=True, exist_ok=True)
    inventory = mini_inventory(
        pin_commit=pin_commit,
        provider_version=provider_version,
        schemas=schemas,
        actions=actions,
    )
    payload = inventory_bytes(inventory)
    (coverage / "inventory.json").write_bytes(payload)
    digest = _sha256(payload)
    (coverage / "dispositions.json").write_text(
        json.dumps(
            {
                "schema_version": "mcp-dispositions-v1",
                "pin_commit": pin_commit,
                "inventory_sha256": digest,
                "rows": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (coverage / "manifest.json").write_bytes(manifest_bytes(build_manifest(coverage)))
    return coverage, digest


def schema_hashes_for(tools_list: list[dict[str, Any]]) -> dict[str, str]:
    """Map ``tools/list`` entries to their canonical schema hashes."""
    return {
        str(tool["name"]): schema_sha256(tool["inputSchema"]) for tool in tools_list
    }


def write_pin(
    tmp_path: Path, *, commit: str, venv_python: str, entry_point: str = "src/server.py"
) -> Path:
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(
        json.dumps(
            {
                "schema_version": "mcp-pin-v1",
                "source_url": "https://github.com/samuelgursky/davinci-resolve-mcp",
                "commit": commit,
                "server_mode": "compound",
                "server_entry_point": entry_point,
                "server_protocol": "stdio",
                "venv_python": venv_python,
                "venv_python_version": "3.12.10",
                "repo_python": "3.12.10",
                "advanced_server": {
                    "package": "davinci-resolve-advanced-mcp",
                    "enabled": False,
                },
                "optional_deps": {"ffmpeg": "required"},
                "resolve_preference": "Local",
                "update_check": {
                    "enabled": False,
                    "mechanism": "fixture",
                },
                "pinned_date": "2026-08-22T00:45:25Z",
            }
        ),
        encoding="utf-8",
    )
    return pin_path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "MINI_PROVIDER_VERSION",
    "OTHER_SHA",
    "git_commit_clone",
    "git_head",
    "mini_inventory",
    "schema_hashes_for",
    "static_server_source",
    "write_coverage_pair",
    "write_mini_clone",
    "write_pin",
]
